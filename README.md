# MEV Sandwich Attack Detection & Victim Outreach Bot

A fully automated MEV sandwich attack detection system that monitors on-chain DEX swaps, identifies victims, and pushes diagnostic alerts via Telegram and ntfy — zero human intervention required.

## Features

- **Real-time Sandwich Detection** — Monitors Uniswap V2/V3 Swap events on every new block
- **Victim Address Extraction** — Automatically identifies the sandwiched victim's wallet
- **Web3 Identity Resolution** — ENS reverse lookup + Farcaster/Lens social account discovery
- **Zero-Friction Victim Outreach** — Auto-mentions the victim on Farcaster the moment an attack is detected (no opt-in, no subscription — the victim sees the alert next time they open Warpcast)
- **HTML Diagnostic Reports** — Auto-generated structured report cards with profit/loss analysis
- **Multi-Channel Push** — Telegram Bot (with inline buttons + Deep Linking) and ntfy.sh
- **Wallet Subscription** — `/watch <address>` zero-barrier self-service protection
- **Deep Link Auto-Binding** — Group members click one button to auto-subscribe victim wallet
- **i18n Architecture** — All user-facing strings stored in a JSON locale file (`src/locales/en.json`)

## Architecture

```
New Block → Swap Event Parse → Sandwich Detect → Victim Extract
    → ENS Reverse Lookup + Farcaster/Lens
    → HTML Report Generate
    → JSON Detection Save
    → ntfy Public Broadcast
    → Subscriber Targeted Notify
    → Telegram Command Handler (/watch /unwatch /status)
```

## Quick Start

### Prerequisites

- Python 3.10+
- An Ethereum RPC endpoint (e.g., [DRPC](https://drpc.org) public node)
- A Telegram Bot token (from [@BotFather](https://t.me/BotFather))
- (Optional) [Neynar API key](https://neynar.com) for Farcaster lookups
- (Optional) A Neynar **signer UUID** (also from Neynar) to enable auto-mentioning victims on Farcaster. Without it, the bot can look up victims but cannot post casts to them.

### Installation

```bash
git clone https://github.com/elandros1/mev-bot.git
cd mev-bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your RPC URL, Telegram token, and ntfy topic
```

### Configuration (.env)

```env
RPC_URL=https://eth.drpc.org
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
NTFY_TOPIC=mev-sandwich-alerts
# Optional
NEYNAR_API_KEY=             # Farcaster profile lookup
NEYNAR_SIGNER_UUID=         # Farcaster auto-cast (write access)
TELEGRAM_API_BASE=          # Custom proxy for Telegram API
HTTP_PORT=8080              # Report HTTP server port
```

### Run

```bash
python main.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/watch <address>` | Subscribe to wallet monitoring |
| `/unwatch <address>` | Unsubscribe |
| `/status` | View your subscriptions |
| `/stats` | View global statistics |
| `/help` | Show help message |

**Deep Linking**: Click the "Watch This Wallet" button on any alert card to auto-subscribe the victim's address — no manual input needed.

## Deployment

### GitHub Actions (Free Cloud)

The repo includes a GitHub Actions workflow (`.github/workflows/mev-bot.yml`) that runs the bot in the cloud. Configure repository Secrets:

- `RPC_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`

Then trigger via Actions page → "Run workflow".

### VPS (24/7)

```bash
# Using systemd or screen
python main.py &
```

## i18n Architecture

All user-facing strings are stored in a JSON locale file:

```
src/locales/
└── en.json    # English strings
```

The `src/i18n.py` module provides:
- `t(lang, key)` — Get a localized string by dotted key
- `get_subscriber_message(lang, report, url)` — Build a subscriber alert message

## Project Structure

```
mev-bot/
├── main.py                     # Main entry point
├── requirements.txt
├── .env.example
├── .github/workflows/
│   └── mev-bot.yml             # GitHub Actions workflow
├── src/
│   ├── analyzer.py             # Sandwich detection engine
│   ├── tg_bot.py               # Telegram + ntfy + command handler
│   ├── report_generator.py     # HTML report card generator
│   ├── subscription_manager.py # SQLite wallet subscription
│   ├── victim_outreach.py      # ENS + Farcaster + Lens lookup
│   ├── i18n.py                 # Internationalization module
│   └── locales/
│       └── en.json             # English locale
├── reports/                    # Generated HTML reports
├── detections/                 # JSON detection records
└── telegram-proxy-worker.js    # Cloudflare Worker proxy (optional)
```

## Tech Stack

- **Python 3.10+** with web3.py, requests, python-dotenv
- **Uniswap V2/V3** Swap event parsing
- **Telegram Bot API** with Deep Linking + Inline Keyboards
- **ntfy.sh** for free push notifications
- **ENS Registry** on-chain reverse lookup
- **Farcaster** (Neynar API) + **Lens Protocol** (GraphQL) social lookup
- **SQLite** for lightweight subscription storage
- **GitHub Actions** for free cloud deployment

## License

MIT
