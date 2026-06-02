# Kalshi API Reference — Key Facts for Agents

*Source: https://docs.kalshi.com/ — verified 2026-06-02*

---

## Base URLs

| Environment | REST | WebSocket |
|---|---|---|
| Demo | `https://demo-api.kalshi.co/trade-api/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |
| Production | `https://external-api.kalshi.com/trade-api/v2` | `wss://external-api.kalshi.com/trade-api/ws/v2` |

**Note:** The legacy production URL `api.elections.kalshi.com` still responds but `external-api.kalshi.com` is the canonical one. Despite the name, the production API covers ALL Kalshi markets — not just elections.

---

## Authentication

Every request needs three headers:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | Your API Key ID |
| `KALSHI-ACCESS-TIMESTAMP` | Current time in milliseconds (string) |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded RSA-PSS SHA-256 signature |

**String to sign:** `timestamp + HTTP_METHOD + path` (path has no query parameters)

**Algorithm:** RSA-PSS with SHA-256, MGF1-SHA256 padding, salt length = digest length (32 bytes)

The same auth headers are used for WebSocket connections — passed in the HTTP upgrade request.

---

## Rate Limits (Basic Tier)

| Budget | Tokens/sec |
|---|---|
| Read (GET requests) | 200 |
| Write (orders, cancellations) | 100 |

**Default token cost: 10 tokens per request.** Effective rates on Basic:
- Read: **20 requests/second** (200 ÷ 10)
- Write: **10 order placements/second** (100 ÷ 10)
- Cancellations and single-order reads cost 2 tokens → up to 50/sec

**Implication for polling:** Polling 20 markets every second costs 20 × 10 = 200 tokens — hitting the read ceiling exactly. Use WebSocket for any market you're actively watching; reserve REST budget for scanner sweeps.

Write budget allows 2-second bursting accumulation. Over-limit returns `429` with no retry headers.

---

## WebSocket Channels

### Private channels (require auth)
| Channel | What it delivers |
|---|---|
| `orderbook_delta` | Incremental order book changes (snapshot on subscribe, then deltas) |
| `fill` | Your order fills in real-time — use instead of polling `/portfolio/fills` |
| `market_positions` | Your position changes |
| `order_group_updates` | Order group lifecycle |

### Public channels (auth required for connection, no per-channel check)
| Channel | What it delivers |
|---|---|
| `ticker` | Real-time `yes_bid_dollars`, `yes_ask_dollars` per market — replaces REST market polling |
| `trade` | Public trade feed (all fills in the market) |
| `market_lifecycle_v2` | Market open/close/settle events |

**Key insight:** The `ticker` channel pushes bid/ask on every change. Subscribing to `ticker` for your watched markets eliminates the need to poll `GET /markets/{ticker}` entirely.

---

## WebSocket Message Envelope

All messages follow this structure:

```json
{
  "type": "message_type",
  "sid": 1,
  "seq": 42,
  "msg": {
    "market_ticker": "...",
    ...payload fields...
  }
}
```

- `sid` — subscription ID (matches the `id` in your subscribe command)
- `seq` — sequence number, increments by 1 per message per subscription. **If you receive seq N+2 after N, you missed a message — re-subscribe to force a fresh snapshot.**
- Error messages have `"msg"` as a plain string, not a dict

### Price format in live feed
Prices are `yes_dollars_fp` / `no_dollars_fp` — arrays of `["price_dollars", "quantity_dollars"]` tuples:
```json
"yes_dollars_fp": [["0.3000", "100.00"], ["0.2800", "50.00"]]
```
`"0.3000"` = 30 cents probability. Convert to integer cents: `round(float(price) * 100)`.

---

## Key Endpoints

| Endpoint | Token cost | Use |
|---|---|---|
| `GET /markets` | 10 | Scanner sweep — paginated, up to 1000/page |
| `GET /markets/{ticker}` | 10 | Single market price + metadata |
| `GET /markets/{ticker}/orderbook` | 10 | REST orderbook snapshot (use WS instead) |
| `GET /markets/orderbooks` | 10 | Batch orderbook for multiple tickers |
| `POST /portfolio/orders` | 10 | Place order |
| `DELETE /portfolio/orders/{id}` | 2 | Cancel order |
| `GET /portfolio/balance` | 10 | Account balance |
| `GET /portfolio/positions` | 10 | Open positions |
| `GET /portfolio/fills` | 10 | Fill history (use `fill` WS channel instead for live) |

---

## Practical Polling Budget

On Basic tier with 20 requests/sec read capacity:
- Scanner sweep of 200 markets (1 paginated call) = 1 call every cycle ✓
- Per-market detail for 10 watched markets = 10 calls ✓
- **Total per cycle: ~11 calls. At 15-second cycles: 0.7 calls/sec. Plenty of headroom.**

Use WebSocket for the markets your agents are actively considering. Use REST scanner only for the sweep.
