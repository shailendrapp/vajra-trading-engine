"""
SPX 0DTE Paper Trading Engine — Configuration
All tunable parameters in one place. Change here, nowhere else.
"""

import os
from typing import List

# ─────────────────────────────────────────────
# TRADIER
# ─────────────────────────────────────────────
TRADIER_API_KEY        = os.getenv("TRADIER_API_KEY", "")
TRADIER_ACCOUNT_ID     = os.getenv("TRADIER_ACCOUNT_ID", "")
TRADIER_PAPER_BASE_URL = "https://sandbox.tradier.com/v1"
TRADIER_LIVE_BASE_URL  = "https://api.tradier.com/v1"   # reserved for future
TRADING_MODE           = "paper"                         # "paper" only for now

# ─────────────────────────────────────────────
# FLASHALPHA  (GEX-based strike selection)
# ─────────────────────────────────────────────
FLASHALPHA_API_KEY      = os.getenv("FLASHALPHA_API_KEY", "")
FLASHALPHA_BASE_URL     = "https://flashalpha.com/v1"
FLASHALPHA_SYMBOL       = "SPX"
FLASHALPHA_DAILY_LIMIT  = 45          # free tier = 50/day; stay under with buffer
FLASHALPHA_CACHE_TTL    = 1800        # re-fetch GEX every 30 minutes (seconds)

# Strike selection: Option A logic
#   1. Find positive GEX wall where delta is in range → use it
#   2. If no wall aligns with delta range → fall back to pure delta
GEX_WALL_MIN_SIZE       = 1_000_000  # ignore walls smaller than this notional
GEX_DELTA_MIN           = 0.10       # short leg delta floor
GEX_DELTA_MAX           = 0.25       # short leg delta ceiling
GEX_WALL_DELTA_FALLBACK = True       # True = fall back to delta if no GEX wall found

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────
# ACCOUNT & SIZING
# ─────────────────────────────────────────────
STARTING_ACCOUNT_EQUITY = float(os.getenv("STARTING_EQUITY", "50000"))
RISK_PCT_PER_TRADE      = 0.01      # 1% of equity per trade
SPREAD_WIDTH_PTS        = 10        # SPX points
MAX_CONCURRENT_SPREADS  = 3
SCALE_UP_AFTER_N_WINS   = 10        # consecutive profitable days before +1 contract

# ─────────────────────────────────────────────
# PROFIT TIERS  (applied to open position set)
# ─────────────────────────────────────────────
# With N open positions:
#   50% of contracts close at TIER_1 profit
#   25% of contracts close at TIER_2 profit
#   25% = free runner → breach or hard close only
PROFIT_TIER_1 = 0.50
PROFIT_TIER_2 = 0.60
SOLO_PROFIT_TARGET = 0.50

# ─────────────────────────────────────────────
# BREACH / STOP RULES
# ─────────────────────────────────────────────
BREACH_DELTA_THRESHOLD   = 0.40
BREACH_PNL_MULTIPLIER    = 2.00
HARD_CLOSE_TIME_PT       = "12:30"

# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────
DAILY_MAX_LOSS_PCT       = 0.02
COOLOFF_MINUTES          = 45
MAX_STOPOUTS_PER_DAY     = 2

# ─────────────────────────────────────────────
# ENTRY GATES
# ─────────────────────────────────────────────
VIX_KILL_SWITCH          = 30.0
VALID_SIGNAL_GRADES      = ["A+", "A"]
NEWS_DAY_SYMBOLS         = ["FOMC", "CPI", "NFP", "PCE", "JOLTS"]

# ─────────────────────────────────────────────
# VIX-BASED POSITION SIZING  (backtest validated)
# ─────────────────────────────────────────────
#   VIX < 20   → 100%   (88–92% win rate zone)
#   VIX 20–25  →  50%   (75% win rate zone)
#   VIX 25–30  →  25%   (47% win rate zone)
#   VIX >= 30  →   0%   (kill switch)
VIX_SIZE_TIERS = [
    (20.0, 1.00),
    (25.0, 0.50),
    (30.0, 0.25),
]

# Entry windows (ET)
ENTRY_WINDOWS_ET = [
    ("10:15", "11:30"),
    ("13:00", "14:30"),
]

# ─────────────────────────────────────────────
# POLLING
# ─────────────────────────────────────────────
POLL_INTERVAL_SECONDS    = 45
MARKET_OPEN_PT           = "06:30"
MARKET_CLOSE_PT          = "13:15"

# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────
DAILY_SUMMARY_TIME_PT    = "13:05"
WEEKLY_SUMMARY_DAY       = 4        # Friday (0=Monday)

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "data/trades.db")
LOG_DIR = os.getenv("LOG_DIR",  "logs/")
