"""
Daily price updater for portfolio tracker.
Reads data.json, fetches latest prices from Yahoo Finance, updates and saves back.
Used by GitHub Actions cron job.
"""
import json
import os
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from datetime import datetime, timezone
from urllib.parse import quote

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

INDICES = [
    ('^DJI', '道琼斯'),
    ('^GSPC', '标普500'),
    ('^RUT', '罗素2000'),
    ('^NDX', '纳斯达克100'),
]


def get_yahoo_crumb():
    """Get Yahoo Finance cookie + crumb for authenticated requests."""
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    # Step 1: Get cookies
    try:
        req = Request('https://fc.yahoo.com', headers={'User-Agent': UA})
        opener.open(req, timeout=10)
    except Exception:
        pass  # Expected - just sets cookies
    # Step 2: Get crumb
    req = Request('https://query2.finance.yahoo.com/v1/test/getcrumb', headers={'User-Agent': UA})
    resp = opener.open(req, timeout=10)
    crumb = resp.read().decode()
    return opener, crumb


def fetch_prices(symbols):
    """Fetch current prices + daily change % for a list of symbols using Yahoo Finance v7 API."""
    if not symbols:
        return {}

    opener, crumb = get_yahoo_crumb()
    sym_str = ','.join(symbols)
    url = f'https://query2.finance.yahoo.com/v7/finance/quote?symbols={sym_str}&crumb={crumb}'
    req = Request(url, headers={'User-Agent': UA})
    resp = opener.open(req, timeout=15)
    data = json.loads(resp.read())

    prices = {}
    for q in data.get('quoteResponse', {}).get('result', []):
        price = q.get('regularMarketPrice')
        if price is not None:
            prices[q['symbol']] = {
                'price': price,
                'dailyPct': round(q.get('regularMarketChangePercent', 0) or 0, 4),
            }
    return prices


def fetch_index_changes(opener, crumb):
    """Fetch 4 major indices with daily/1m/3m/YTD change percentages."""
    symbols = [sym for sym, _ in INDICES]
    name_map = {sym: name for sym, name in INDICES}

    # Get current prices and daily change from v7 quote API
    sym_str = ','.join(symbols)
    url = f'https://query2.finance.yahoo.com/v7/finance/quote?symbols={quote(sym_str, safe=",")}&crumb={crumb}'
    req = Request(url, headers={'User-Agent': UA})
    resp = opener.open(req, timeout=15)
    data = json.loads(resp.read())

    results = {}
    for q in data.get('quoteResponse', {}).get('result', []):
        sym = q.get('symbol')
        if sym not in name_map:
            continue
        results[sym] = {
            'name': name_map[sym],
            'price': q.get('regularMarketPrice', 0),
            'daily': round(q.get('regularMarketChangePercent', 0), 2),
        }

    # Get historical prices for 1m/3m/YTD via v8 chart API
    today = datetime.now(timezone.utc)
    ytd_start = int(datetime(today.year, 1, 1, tzinfo=timezone.utc).timestamp())

    for sym in symbols:
        if sym not in results:
            continue
        try:
            # Fetch 1 year of data to cover all periods
            chart_url = (
                f'https://query2.finance.yahoo.com/v8/finance/chart/{quote(sym, safe="")}?'
                f'period1={ytd_start}&period2={int(today.timestamp())}'
                f'&interval=1d&crumb={crumb}'
            )
            req = Request(chart_url, headers={'User-Agent': UA})
            resp = opener.open(req, timeout=15)
            chart = json.loads(resp.read())

            closes = chart['chart']['result'][0]['indicators']['quote'][0]['close']
            timestamps = chart['chart']['result'][0]['timestamp']
            # Filter out None values and pair with timestamps
            valid = [(t, c) for t, c in zip(timestamps, closes) if c is not None]

            if not valid:
                results[sym]['m1'] = None
                results[sym]['m3'] = None
                results[sym]['ytd'] = None
                continue

            current = results[sym]['price']
            # YTD: first valid close of the year
            ytd_base = valid[0][1]
            results[sym]['ytd'] = round((current - ytd_base) / ytd_base * 100, 2)

            # 1 month (~21 trading days) and 3 months (~63 trading days)
            for key, days in [('m1', 21), ('m3', 63)]:
                if len(valid) >= days:
                    base = valid[-days][1]
                    results[sym][key] = round((current - base) / base * 100, 2)
                else:
                    # Use earliest available
                    base = valid[0][1]
                    results[sym][key] = round((current - base) / base * 100, 2)

        except Exception as e:
            print(f'Warning: Failed to get history for {sym}: {e}')
            results[sym].setdefault('m1', None)
            results[sym].setdefault('m3', None)
            results[sym].setdefault('ytd', None)

    return results


def update_data():
    """Main update routine."""
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # Collect all unique tickers
    tickers = set()
    for account in data.get('accounts', []):
        for h in account.get('holdings', []):
            tickers.add(h['ticker'])

    if not tickers:
        print('No holdings found, nothing to update.')
        return False

    print(f'Fetching prices for {len(tickers)} symbols: {", ".join(sorted(tickers))}')
    prices = fetch_prices(list(tickers))
    print(f'Got prices: {prices}')

    # Fetch index data
    try:
        opener, crumb = get_yahoo_crumb()
        indices = fetch_index_changes(opener, crumb)
        data['indices'] = indices
        print(f'Got indices: {list(indices.keys())}')
    except Exception as e:
        print(f'Warning: Failed to fetch indices: {e}')

    if not prices:
        print('ERROR: Failed to fetch any prices.')
        return False

    # Update holdings
    updated = 0
    for account in data['accounts']:
        for h in account['holdings']:
            if h['ticker'] in prices:
                h['currentPrice'] = prices[h['ticker']]['price']
                h['dailyPct'] = prices[h['ticker']]['dailyPct']
                updated += 1

    # Record history for each account
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if 'history' not in data:
        data['history'] = {}

    for account in data['accounts']:
        acc_id = account['id']
        holdings_val = sum(h.get('shares', 0) * h.get('currentPrice', h.get('costPerShare', 0))
                          for h in account['holdings'])
        total = holdings_val + account.get('cash', 0)

        if acc_id not in data['history']:
            data['history'][acc_id] = []

        hist = data['history'][acc_id]
        existing = next((i for i, h in enumerate(hist) if h['date'] == today), -1)
        if existing >= 0:
            hist[existing]['value'] = total
        else:
            hist.append({'date': today, 'value': total})

        # Keep max 5 years (~1825 days)
        if len(hist) > 1825:
            data['history'][acc_id] = hist[-1825:]

    data['lastUpdate'] = datetime.now(timezone.utc).isoformat()

    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'Updated {updated} holdings. History recorded for {today}.')
    return True


if __name__ == '__main__':
    try:
        success = update_data()
        exit(0 if success else 1)
    except Exception as e:
        print(f'ERROR: {e}')
        exit(1)
