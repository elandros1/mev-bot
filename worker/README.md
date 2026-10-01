# MEV Sandwich Bot — Cloudflare Workers Edition

A TypeScript / Cloudflare Workers port of the MEV sandwich attack detection
& victim outreach system. Runs on Cloudflare's global edge network with
**Cron Triggers** firing every minute — no servers, no GitHub Actions, no
self-hosted process to keep alive.

## Architecture

```
Cron Trigger (every minute)
    |
    v
index.ts scheduled()
    |
    |- Heartbeat (every 5 min) -> ntfy + Telegram status check
    |- Read last scanned block from MevState Durable Object
    |- For each new block (up to 6 per tick):
    |     |- analyzer.fetchSwaps()      -> eth_getLogs(V2+V3 Swap topics)
    |     |- analyzer.detectSandwiches() -> same-block front-run + back-run
    |     |- handleAttack():
    |           |- victim_outreach.identifyVictim()  -> ENS + Farcaster
    |           |- report_generator.generateReportHtml()
    |           |- notifier.send()                   -> Telegram + ntfy
    |           |- victim_outreach.outreachToFarcaster() -> auto-mention
    |           |- state.getSubscribersForAddress()  -> DM subscribers
    |- Persist new block cursor
    |
    v
HTTP /tg webhook -> Bot commands (/watch /unwatch /status /help /stats)
                    + Deep Link auto-binding (start=watch_<address>)
```

## Files

| Path | Purpose |
|---|---|
| `src/index.ts` | Entry point — Cron `scheduled()` + HTTP `fetch()` webhook |
| `src/analyzer.ts` | Sandwich detection (ethers.js port of `src/analyzer.py`) |
| `src/report_generator.ts` | HTML report card (same template as Python) |
| `src/notifier.ts` | Telegram + ntfy card builder with ntfy server fallback |
| `src/victim_outreach.ts` | ENS reverse lookup + Farcaster auto-mention |
| `src/state.ts` | Durable Object: block cursor + subscriptions + stats |
| `src/i18n.ts` + `src/locales/en.json` | English-only locale (per project spec) |
| `wrangler.toml` | Worker config, Cron schedule, DO + KV bindings, vars |

## Notification Channels

### ntfy
Uses **ntfy.private.coffee** as the primary server with automatic fallback to
ntfy.sh. Note: ntfy.sh rate-limits Cloudflare's IP range (HTTP 429), so
ntfy.private.coffee is preferred. Configure via `NTFY_SERVER` var in
`wrangler.toml`.

Subscribe to topic `mev-sandwich-alerts` in the ntfy mobile app to receive
alerts.

### Telegram
Bot: **@my_mev_xu_bot** (MEV Guardian)

**Important**: Before the bot can message you, you must send it a message
(e.g. `/start`). If Telegram returns "chat not found", message the bot first.

Verify setup:
```bash
# Check that the bot can reach you
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

## Local Development

```bash
npm install
npx wrangler dev            # local dev server
```

## Deploy

```bash
# 1. Login (one time)
npx wrangler login

# 2. Set secrets
npx wrangler secret put RPC_URL
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put NTFY_TOPIC
# Optional:
npx wrangler secret put NEYNAR_API_KEY
npx wrangler secret put NEYNAR_SIGNER_UUID

# 3. Deploy (creates the Worker, Durable Object namespace, KV, and Cron Trigger)
npx wrangler deploy

# 4. (Optional) Set Telegram webhook for /watch commands
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<worker>.workers.dev/tg"
```

## Cron Schedule

In `wrangler.toml`:

```toml
[triggers]
crons = ["* * * * *"]          # every minute
# or "*/15 * * * *"            # every 15 minutes
# or "*/5 * * * *"             # every 5 minutes
```

Cloudflare's minimum cron granularity is 1 minute. Each tick scans up to 6
new blocks (configurable in `index.ts` via `endBlock`). Ethereum produces
~5 blocks/min, so 6 blocks/tick keeps the bot roughly in sync.

## Diagnostics

The Worker stores runtime diagnostics in a KV namespace (`DIAG` binding):

| Key | Description |
|---|---|
| `heartbeat` | Latest tick diagnostics (block cursor, attack count, etc.) |
| `hb_send_result` | Last heartbeat send results (ntfy + Telegram status) |
| `last_hb_minute` | Throttle timestamp for 5-min heartbeat cadence |

Read via Cloudflare API:
```bash
curl -X GET "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/storage/kv/namespaces/<KV_ID>/values/heartbeat" \
  -H "Authorization: Bearer <API_TOKEN>"
```

## Cost

Cloudflare Workers free tier:
- 100,000 Cron invocations / day
- 10 ms CPU per invocation (enough for 6 block scans)

This is well within the free tier for the bot's real workload.

## License

Same as the parent project.
