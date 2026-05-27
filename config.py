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
TRADIER_LIVE_BASE_URL  = "https://api.tradier.com/v1"
TRADING_MODE           = "paper"

# ─────────────────────────────────────────────
# FLASHALPHA  (GEX-based strike selection)
# ─────────────────────────────────────────────
FLASHALPHA_API_KEY      = os.getenv("FLASHALPHA_API_KEY", "")
FLASHALPHA_BASE_URL     = "https://flashalpha.com/v1"
FLASHALPHA_SYMBOL       = "SPX"
FLASHALPHA_DAILY_LIMIT  = 45
FLASHALPHA_CACHE_TTL    = 1800

GEX_WALL_MIN_SIZE       = 1_000_000
GEX_DELTA_MIN           = 0.10
GEX_DELTA_MAX           = 0.25
GEX_WALL_DELTA_FALLBACK = True

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────
# ACCOUNT & SIZING
# ─────────────────────────────────────────────
STARTING_ACCOUNT_EQUITY = float(os.getenv("STARTING_EQUITY", "50000"))
RISK_PCT_PER_TRADE      = 0.01
SPREAD_WIDTH_PTS        = 10
MAX_CONCURRENT_SPREADS  = 3
SCALE_UP_AFTER_N_WINS   = 10

# ─────────────────────────────────────────────
# IC CONTRACT STRUCTURE
# ─────────────────────────────────────────────
# Every IC signal opens exactly 3 contracts with fixed exit rules:
#   Contract 1 → close at 50% of credit received
#   Contract 2 → close at 70% of credit received
#   Contract 3 → free runner (breach or hard close only)
#
# Applies to Iron Condor only.
# Bear Call / Bull Put use standard single-contract sizing.
IC_CONTRACTS            = 3          # always 3 for IC
IC_CONTRACT_1_TARGET    = 0.50       # 50% profit
IC_CONTRACT_2_TARGET    = 0.70       # 70% profit
# Contract 3 = free runner, no profit target

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
#   VIX < 20   → 100%  (88–92% win rate zone)
#   VIX 20–25  →  50%  (75% win rate zone)
#   VIX 25–30  →  25%  (47% win rate zone)
#   VIX >= 30  →   0%  (kill switch)
#
# Note: IC always opens IC_CONTRACTS=3. The VIX multiplier
# reduces this: VIX 20-25 → 2 contracts, VIX 25-30 → 1 contract.
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
WEEKLY_SUMMARY_DAY       = 4

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "data/trades.db")
LOG_DIR = os.getenv("LOG_DIR",  "logs/")
