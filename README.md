# Argus Trading Engine 🦅

Automated SPX 0DTE paper trading execution engine.  
Receives signals from **SPX 0DTE Pro V9** (TradingView), executes via **Tradier sandbox API**, monitors positions every 45 seconds, and delivers daily/weekly P&L summaries via Telegram.

> **Paper trading only.** Live trading mode is reserved for a future release after extended paper validation.

---

## Architecture

```
TradingView (V9 Signal)
        │
        ▼  (Telegram command — V1)
┌─────────────────────────────────┐
│       Argus Trading Engine      │
│                                 │
│  engine.py  ← orchestrator      │
│  ├── position_monitor  (45s)    │
│  ├── trade_entry                │
│  ├── risk_manager               │
│  └── telegram_bot               │
│                                 │
│  core/                          │
│  ├── tradier.py  (API layer)    │
│  └── database.py (SQLite)       │
└─────────────────────────────────┘
        │
        ▼
  Tradier Sandbox API
```

---

## File Structure

```
argus-trading-engine/
├── engine.py                  # Main orchestrator — start here
├── config.py                  # All tunable parameters
├── requirements.txt
├── .env.example               # Copy to .env and fill in secrets
├── .gitignore
│
├── core/
│   ├── database.py            # SQLite schema + all persistence
│   ├── tradier.py             # Tradier API wrapper (quotes, Greeks, orders)
│   └── risk_manager.py        # Entry gates, circuit breaker, sizing
│
├── modules/
│   ├── position_monitor.py    # 45s polling loop — tier exits + breach detection
│   ├── trade_entry.py         # Signal intake, strike selection, order placement
│   └── telegram_bot.py        # Summaries + /commands
│
├── data/                      # SQLite DB lives here (gitignored)
├── logs/                      # Engine logs (gitignored)
└── tests/                     # Unit tests (coming in next phase)
```

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/argus-trading-engine.git
cd argus-trading-engine
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example .env
# Edit .env with your Tradier + Telegram credentials
```

See **Secrets** section below for where to get each value.

### 3. Run dry-run (no orders placed)

```bash
python engine.py --dry-run
```

### 4. Run paper trading

```bash
python engine.py
```

Engine runs **6:30 AM – 1:15 PM PT on weekdays**. Outside those hours it exits cleanly.

---

## Secrets Required

| Secret | Where to Get It | Notes |
|---|---|---|
| `TRADIER_API_KEY` | [sandbox.tradier.com](https://sandbox.tradier.com) → API Access | Use **sandbox** key only |
| `TRADIER_ACCOUNT_ID` | Tradier sandbox dashboard → Account number | Numeric string |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` | Reuse CSP scanner bot or create new |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot) | Your personal chat ID |
| `STARTING_EQUITY` | Set to your Tradier paper account balance | Default: `50000` |

---

## Telegram Commands

| Command | Action |
|---|---|
| `/enter IC` | Enter Iron Condor (grade A assumed) |
| `/enter IC A+` | Enter Iron Condor, grade A+ |
| `/enter BEAR_CALL` | Enter Bear Call Spread |
| `/enter BULL_PUT` | Enter Bull Put Spread |
| `/status` | Show open positions + today's P&L |
| `/pause` | Halt new entries (existing positions stay open) |
| `/resume` | Re-enable entries |
| `/close_all` | Emergency close all open positions |
| `/help` | List all commands |

---

## Trading Rules (enforced in code)

| Rule | Value |
|---|---|
| Poll interval | Every 45 seconds |
| Max concurrent spreads | 3 |
| Profit tier 1 (50% of positions) | 50% of credit |
| Profit tier 2 (25% of positions) | 60% of credit |
| Free runner (25% of positions) | Breach or hard close only |
| Solo position target | 50% of credit |
| Breach — delta | Short leg delta ≥ 0.40 |
| Breach — P&L | Debit to close ≥ 2× credit received |
| Hard close | 12:30 PM PT — all positions |
| Daily max loss (circuit breaker) | 2% of account equity |
| Cooling-off after stop-out | 45 minutes |
| 2nd stop-out | No more entries that day |
| VIX kill switch | VIX ≥ 30 |
| Valid signal grades | A+ and A only |
| Entry windows | 10:15–11:30 AM ET and 1:00–2:30 PM ET |

---

## Related Projects

| Project | Description |
|---|---|
| **SPX 0DTE Pro V9** | TradingView Pine Script signal generator (separate repo) |
| **CSP Scanner** | GitHub Actions–based cash-secured put scanner |

---

## Roadmap

- [ ] V2: TradingView webhook → auto signal intake (no manual `/enter`)
- [ ] V2: Economic calendar API integration (replace manual news day flag)
- [ ] V2: Greeks snapshot at entry for post-trade analysis dashboard
- [ ] V3: Live trading mode (after 60+ paper trading days validated)
- [ ] V3: React P&L dashboard (extend existing trade journal)
