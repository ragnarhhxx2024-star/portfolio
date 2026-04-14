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

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')


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
    """Fetch current prices for a list of symbols using Yahoo Finance v7 API."""
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
            prices[q['symbol']] = price
    return prices


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

    if not prices:
        print('ERROR: Failed to fetch any prices.')
        return False

    # Update holdings
    updated = 0
    for account in data['accounts']:
        for h in account['holdings']:
            if h['ticker'] in prices:
                h['currentPrice'] = prices[h['ticker']]
                updated += 1

    # Record history for each account
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if 'history' not in data:
        data['history'] = {}

    for account in data['accounts']:
        acc_id = account['id']
        total = sum(h.get('shares', 0) * h.get('currentPrice', h.get('costPerShare', 0))
                    for h in account['holdings'])

        if acc_id not in data['history']:
            data['history'][acc_id] = []

        hist = data['history'][acc_id]
        existing = next((i for i, h in enumerate(hist) if h['date'] == today), -1)
        if existing >= 0:
            hist[existing]['value'] = total
        else:
            hist.append({'date': today, 'value': total})

        # Keep max 365 days
        if len(hist) > 365:
            data['history'][acc_id] = hist[-365:]

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
