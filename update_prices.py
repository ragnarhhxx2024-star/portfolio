"""
Daily price updater for portfolio tracker.
Reads data.json, fetches latest prices from Yahoo Finance, updates and saves back.
Used by GitHub Actions cron job.
"""
import json
import os
import re
import time
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from urllib.error import URLError, HTTPError
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


def _http_get(url, headers=None, timeout=15, opener=None):
    """Simple HTTP GET helper that returns decoded text, or raises."""
    h = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
         'Accept-Language': 'en-US,en;q=0.9'}
    if headers:
        h.update(headers)
    req = Request(url, headers=h)
    if opener is None:
        resp = urlopen(req, timeout=timeout)
    else:
        resp = opener.open(req, timeout=timeout)
    return resp.read().decode('utf-8', errors='replace')


def fetch_short_float_finviz(ticker):
    """Primary: Finviz quote page 'Short Float' field. Returns percent as float or None."""
    try:
        html = _http_get(f'https://finviz.com/quote.ashx?t={quote(ticker)}&p=d')
        # Structure: Short Float</a></div></td><td ...><div ...><a ...><b>2.25%</b>...
        # Must extract from inside <b> tag — simple % regex matches CSS class values like w-[8%]
        m = re.search(r'Short Float[\s\S]{0,800}?<b[^>]*>\s*([\d.]+)\s*%', html)
        if m:
            return round(float(m.group(1)), 2)
    except Exception as e:
        print(f'  finviz {ticker}: {e}')
    return None


def fetch_short_float_yahoo(ticker, opener, crumb):
    """Fallback 1: Yahoo quoteSummary defaultKeyStatistics.shortPercentOfFloat."""
    try:
        url = (f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker)}'
               f'?modules=defaultKeyStatistics&crumb={crumb}')
        raw = _http_get(url, opener=opener)
        j = json.loads(raw)
        stats = j.get('quoteSummary', {}).get('result', [{}])[0].get('defaultKeyStatistics', {})
        spf = stats.get('shortPercentOfFloat', {})
        # Yahoo returns decimal (e.g. 0.0523 = 5.23%)
        raw_val = spf.get('raw')
        if raw_val is not None:
            return round(raw_val * 100, 2)
    except Exception as e:
        print(f'  yahoo {ticker}: {e}')
    return None


def fetch_short_float_stockanalysis(ticker):
    """Fallback 2: stockanalysis.com statistics page (Next.js inline JSON).
    Data is embedded as: shortFloat",title:"Short % of Float",value:"12.34%"
    """
    try:
        html = _http_get(f'https://stockanalysis.com/stocks/{quote(ticker.lower())}/statistics/')
        m = re.search(r'shortFloat"[\s\S]{0,120}?value:"([\d.]+)\s*%"', html)
        if m:
            return round(float(m.group(1)), 2)
        # Also try the overview page as a backup inside this source
        html = _http_get(f'https://stockanalysis.com/stocks/{quote(ticker.lower())}/')
        m = re.search(r'shortFloat"[\s\S]{0,120}?value:"([\d.]+)\s*%"', html)
        if m:
            return round(float(m.group(1)), 2)
    except Exception as e:
        print(f'  stockanalysis {ticker}: {e}')
    return None


def fetch_short_floats(tickers, opener, crumb):
    """Fetch short float % for a list of tickers using fallback chain.
    Returns dict {ticker: {value: float, source: str}}.
    """
    results = {}
    for i, t in enumerate(tickers):
        # Try sources in order
        val = fetch_short_float_finviz(t)
        source = 'finviz'
        if val is None:
            val = fetch_short_float_yahoo(t, opener, crumb)
            source = 'yahoo'
        if val is None:
            val = fetch_short_float_stockanalysis(t)
            source = 'stockanalysis'
        if val is not None:
            results[t] = {'value': val, 'source': source}
            print(f'  [{source}] {t}: {val}%')
        else:
            print(f'  [none] {t}: no data')
        # Be polite - small delay between requests to avoid rate limiting
        if i < len(tickers) - 1:
            time.sleep(0.8)
    return results


def fetch_spx_history(opener, crumb, years=5):
    """Fetch ^GSPC daily closes for the given number of years.
    Returns list of {date: 'YYYY-MM-DD', value: float}, sorted ascending.
    """
    today = datetime.now(timezone.utc)
    period1 = int((today.timestamp()) - years * 366 * 86400)
    period2 = int(today.timestamp())
    url = (f'https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC?'
           f'period1={period1}&period2={period2}&interval=1d&crumb={crumb}')
    req = Request(url, headers={'User-Agent': UA})
    resp = opener.open(req, timeout=20)
    chart = json.loads(resp.read())
    result = chart['chart']['result'][0]
    timestamps = result['timestamp']
    closes = result['indicators']['quote'][0]['close']
    out = []
    for t, c in zip(timestamps, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%d')
        out.append({'date': d, 'value': round(c, 2)})
    out.sort(key=lambda x: x['date'])
    return out


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
    opener, crumb = None, None
    try:
        opener, crumb = get_yahoo_crumb()
        indices = fetch_index_changes(opener, crumb)
        data['indices'] = indices
        print(f'Got indices: {list(indices.keys())}')
    except Exception as e:
        print(f'Warning: Failed to fetch indices: {e}')

    # Fetch S&P 500 5y history for benchmark overlay on the trend chart
    try:
        if opener is None or crumb is None:
            opener, crumb = get_yahoo_crumb()
        spx_hist = fetch_spx_history(opener, crumb, years=5)
        if spx_hist:
            data['spxHistory'] = spx_hist
            print(f'Got SPX history: {len(spx_hist)} daily points')
    except Exception as e:
        print(f'Warning: Failed to fetch SPX history: {e}')

    if not prices:
        print('ERROR: Failed to fetch any prices.')
        return False

    # Fetch short float % for each ticker (skip ETFs/indices — they usually lack the metric)
    # Collect tickers with STOCK type only
    stock_tickers = sorted({h['ticker'] for account in data['accounts']
                            for h in account.get('holdings', [])
                            if (h.get('type') or 'STOCK').upper() == 'STOCK'})
    short_floats = {}
    if stock_tickers:
        print(f'Fetching short float for {len(stock_tickers)} stocks...')
        try:
            # Ensure we have a Yahoo opener/crumb for fallback
            if opener is None or crumb is None:
                opener, crumb = get_yahoo_crumb()
            short_floats = fetch_short_floats(stock_tickers, opener, crumb)
        except Exception as e:
            print(f'Warning: short float fetch failed: {e}')

    # Update holdings
    updated = 0
    for account in data['accounts']:
        for h in account['holdings']:
            if h['ticker'] in prices:
                h['currentPrice'] = prices[h['ticker']]['price']
                h['dailyPct'] = prices[h['ticker']]['dailyPct']
                updated += 1
            if h['ticker'] in short_floats:
                h['shortFloat'] = short_floats[h['ticker']]['value']
                h['shortFloatSource'] = short_floats[h['ticker']]['source']

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
