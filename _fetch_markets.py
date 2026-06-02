from kalshi_auth import KalshiClient
import csv

client = KalshiClient.from_env('prod')

all_events = []
cursor = ''
while True:
    params = {'status': 'open', 'limit': 200, 'with_nested_markets': 'true'}
    if cursor:
        params['cursor'] = cursor
    r = client.get('/events', params)
    events = r.get('events', [])
    all_events.extend(events)
    cursor = r.get('cursor', '')
    if not cursor or not events:
        break
    if len(all_events) % 2000 == 0:
        print(f'  ...{len(all_events)} events so far', flush=True)

print(f'Total open events: {len(all_events)}')

all_markets = []
for e in all_events:
    for m in e.get('markets', []):
        m['category'] = e.get('category', '')
        m['event_title'] = e.get('title', '')
        all_markets.append(m)

print(f'Total markets: {len(all_markets)}')

all_markets.sort(key=lambda m: float(m.get('volume_fp', 0) or 0), reverse=True)

fields = [
    'ticker', 'title', 'event_ticker', 'event_title', 'category',
    'volume_fp', 'volume_24h_fp', 'open_interest_fp',
    'last_price_dollars', 'previous_price_dollars',
    'yes_bid_dollars', 'yes_ask_dollars',
    'yes_bid_size_fp', 'yes_ask_size_fp',
    'previous_yes_bid_dollars', 'previous_yes_ask_dollars',
    'no_bid_dollars', 'no_ask_dollars',
    'yes_sub_title', 'no_sub_title',
    'liquidity_dollars', 'open_time', 'close_time',
    'mve_selected_legs',
]

out_path = '/Users/llewis/ai_week/live_markets.csv'
with open(out_path, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(all_markets)

print(f'Written {len(all_markets)} rows to {out_path}')
print('Top 10 by volume:')
for m in all_markets[:10]:
    print(f"  {m['ticker']} | vol={float(m.get('volume_fp',0) or 0):>10,.0f} | 24h={float(m.get('volume_24h_fp',0) or 0):>8,.0f} | {m['title'][:55]}")
