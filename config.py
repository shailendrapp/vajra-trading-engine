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
FLASHALPHA_BASE_URL     = "https://lab.flashalpha.com/v1"
# FlashAlpha /stock/spx/summary supports SPX directly
FLASHALPHA_SYMBOL       = "SPX"
FLASHALPHA_DAILY_LIMIT  = 45
FLASHALPHA_CACHE_TTL    = 1800

GEX_WALL_MIN_SIZE       = 1_000_000
GEX_DELTA_MIN           = 0.10
GEX_DELTA_MAX           = 0.25
GEX_WALL_DELTA_FALLBACK = True

# ─────────────────────────────────────────────
# ANTHROPIC  (Claude verdict for BIC alerts)
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

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
HARD_CLOSE_TIME_PT       = "13:00"   # full session — 4:00 PM ET natural market close
FOMC_EXIT_TIME_PT        = "10:30"   # exit all positions before 2 PM ET FOMC announcement

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

# ─────────────────────────────────────────────
# BIC SCANNER  (Breakeven Iron Condor)
# ─────────────────────────────────────────────
# Dynamic wing width based on VIX (backtest validated)
#   VIX < 20  → 25pt wings  (94.3% win rate)
#   VIX 20-25 → 30pt wings  (98.7% win rate)
#   VIX 25-30 → 35pt wings  (98.8% win rate)
BIC_WING_TIERS = [
    (20.0, 25),
    (25.0, 30),
    (30.0, 35),
]

# Adaptive delta — target changes based on ATM IV level
# Low IV → lower delta → strikes go further OTM → safer
# High IV → higher delta → can afford closer strike → more premium
# Backtest validated: +$33K improvement over fixed 0.09 target
BIC_SHORT_DELTA_TARGET = 0.09   # default (VIX 15-20 range)
BIC_SHORT_DELTA_MIN    = 0.04   # minimum delta (very low IV days)
BIC_SHORT_DELTA_MAX    = 0.12   # maximum acceptable delta

# Adaptive delta tiers by ATM IV
# atm_iv = VIX/100 (no premium — raw IV)
# Adaptive delta tiers — original validated thresholds
# Delta target based on ATM IV — lower IV → lower delta → further OTM
BIC_ADAPTIVE_DELTA = [
    (0.11, 0.05),   # atm_iv < 11% → delta 0.05 (very far OTM)
    (0.13, 0.06),   # atm_iv < 13% → delta 0.06 (low IV)
    (0.16, 0.09),   # atm_iv < 16% → delta 0.09 (normal — standard BIC)
    (0.99, 0.12),   # atm_iv ≥ 16% → delta 0.12 (high IV, more premium)
]

# EM validation settings
BIC_EM_MULT          = 0.65    # min distance = EM × 0.65 (relaxed from 0.80)
BIC_TRADING_HOURS    = 6.5     # 9:30 AM - 4:00 PM ET
BIC_USE_TIME_ADJ_EM  = True    # use time-adjusted EM per window (shrinks through day)
BIC_MIN_CREDIT         = 0.50   # minimum $0.50 credit per IC ($50/contract)

# Credit-to-VIX sanity check — skip if credit too low for current VIX
# Prevents entering thin setups where 2x breach fires almost immediately
# Today's example: VIX 18.3, credit $0.55 → should be blocked
BIC_CREDIT_VIX_TIERS = [
    (17.0, 0.50),   # VIX < 17  → min credit $0.50 (normal)
    (20.0, 0.70),   # VIX 17-20 → min credit $0.70
    (30.0, 1.00),   # VIX 20-30 → min credit $1.00
]
BIC_VIX_FLOOR          = 12.0   # skip if VIX < 12 (credit too thin)

# BIC scan schedule — entry windows (ET)
# System scans at these times and auto-enters if conditions met
BIC_ENTRY_WINDOWS_ET = [
    "10:15",   # Window 1 — post open-range
    "11:15",   # Window 2
    "12:15",   # Window 3 — best theta window
    "13:15",   # Window 4
    "14:15",   # Window 5 — final entry (leaves 75min before 12:30 PT close)
]

# News events to skip (checked against economic calendar)
BIC_NEWS_EVENTS = ["FOMC", "CPI", "NFP", "PCE", "JOLTS", "FOMC_MINUTES"]
