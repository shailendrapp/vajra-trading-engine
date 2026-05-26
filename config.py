"""
SPX 0DTE Paper Trading Engine — Configuration
All tunable parameters in one place. Change here, nowhere else.
"""

import os
from dataclasses import dataclass, field
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
PROFIT_TIER_1 = 0.50   # 50% of credit received
PROFIT_TIER_2 = 0.60   # 60% of credit received
# Tier 3 = free runner, no profit target

# Solo position (only 1 open): always close at TIER_1
SOLO_PROFIT_TARGET = 0.50

# ─────────────────────────────────────────────
# BREACH / STOP RULES
# ─────────────────────────────────────────────
BREACH_DELTA_THRESHOLD   = 0.40     # short leg delta abs value
BREACH_PNL_MULTIPLIER    = 2.00     # debit_to_close >= 2x credit_received → stop
HARD_CLOSE_TIME_PT       = "12:30"  # Pacific Time — ALL positions closed

# ─────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────
DAILY_MAX_LOSS_PCT       = 0.02     # 2% of account equity
COOLOFF_MINUTES          = 45       # after any stop-out
MAX_STOPOUTS_PER_DAY     = 2        # 2nd stop-out = no more entries today

# ─────────────────────────────────────────────
# ENTRY GATES
# ─────────────────────────────────────────────
VIX_KILL_SWITCH          = 30.0
VALID_SIGNAL_GRADES      = ["A+", "A"]
NEWS_DAY_SYMBOLS         = ["FOMC", "CPI", "NFP", "PCE", "JOLTS"]

# ─────────────────────────────────────────────
# VIX-BASED POSITION SIZING  (backtest validated)
# ─────────────────────────────────────────────
# Win rate drops ~17pp when VIX crosses 20. Size down proportionally.
#
#   VIX < 20   → 100% of calculated contracts  (88–92% win rate zone)
#   VIX 20–25  →  50% of calculated contracts  (75% win rate zone)
#   VIX 25–30  →  25% of calculated contracts  (47% win rate — barely worth it)
#   VIX >= 30  →   0% — kill switch blocks entry entirely
#
# Format: list of (vix_threshold, size_multiplier)
# Engine applies the multiplier for the first tier where VIX < threshold.
VIX_SIZE_TIERS = [
    (20.0, 1.00),
    (25.0, 0.50),
    (30.0, 0.25),
]

# Entry windows (ET) — tuples of (HH:MM, HH:MM)
ENTRY_WINDOWS_ET = [
    ("10:15", "11:30"),
    ("13:00", "14:30"),
]

# ─────────────────────────────────────────────
# POLLING
# ─────────────────────────────────────────────
POLL_INTERVAL_SECONDS    = 45       # position monitor heartbeat
MARKET_OPEN_PT           = "06:30"
MARKET_CLOSE_PT          = "13:15"  # system shuts down after summaries sent

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
