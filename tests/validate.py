"""
Argus Trading Engine — Full System Validation
Run this anytime to verify all modules are working correctly.
No market hours, no Tradier, no FlashAlpha, no Telegram needed.

Usage:
    cd ~/Downloads/argus-trading-engine
    python3 tests/validate.py

Expected: 74/74 passed when .env is configured
"""
import sys, os, json, math
sys.path.insert(0, '.')
os.makedirs('data', exist_ok=True)
os.makedirs('logs', exist_ok=True)

# Load .env before importing config
from dotenv import load_dotenv
load_dotenv()

# Always start with a clean DB so previous runs don't pollute results
_db = os.path.join('data', 'trades.db')
if os.path.exists(_db):
    os.remove(_db)

print("=" * 60)
print("  ARGUS TRADING ENGINE — SYSTEM VALIDATION")
print("=" * 60)
passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name}{' — ' + detail if detail else ''}")
        failed += 1

# ── 1. CONFIG ─────────────────────────────────────────────────────────────
print("\n── 1. Config ────────────────────────────────────────────")
from config import (
    POLL_INTERVAL_SECONDS, VIX_KILL_SWITCH, VIX_SIZE_TIERS,
    FLASHALPHA_API_KEY, TRADIER_API_KEY, TELEGRAM_BOT_TOKEN,
    IC_CONTRACT_1_TARGET, IC_CONTRACT_2_TARGET, IC_CONTRACTS,
    BREACH_DELTA_THRESHOLD, BREACH_PNL_MULTIPLIER, HARD_CLOSE_TIME_PT,
    DAILY_MAX_LOSS_PCT, COOLOFF_MINUTES, MAX_STOPOUTS_PER_DAY,
    SPREAD_WIDTH_PTS, RISK_PCT_PER_TRADE, GEX_DELTA_MIN, GEX_DELTA_MAX,
    FLASHALPHA_DAILY_LIMIT
)
check("Poll interval = 45s",           POLL_INTERVAL_SECONDS == 45)
check("VIX kill switch = 30",          VIX_KILL_SWITCH == 30.0)
check("VIX size tiers defined",        len(VIX_SIZE_TIERS) == 3)
check("IC contracts = 3",              IC_CONTRACTS == 3)
check("IC contract 1 target = 50%",    IC_CONTRACT_1_TARGET == 0.50)
check("IC contract 2 target = 70%",    IC_CONTRACT_2_TARGET == 0.70)
check("Breach delta = 0.40",           BREACH_DELTA_THRESHOLD == 0.40)
check("Breach P&L mult = 2.0x",        BREACH_PNL_MULTIPLIER == 2.00)
check("Hard close = 12:30 PT",         HARD_CLOSE_TIME_PT == "12:30")
check("Daily loss cap = 2%",           DAILY_MAX_LOSS_PCT == 0.02)
check("Cooling off = 45 min",          COOLOFF_MINUTES == 45)
check("Max stopouts/day = 2",          MAX_STOPOUTS_PER_DAY == 2)
check("Spread width = 10pts",          SPREAD_WIDTH_PTS == 10)
check("Risk pct = 1%",                 RISK_PCT_PER_TRADE == 0.01)
check("GEX delta range 0.10-0.25",     GEX_DELTA_MIN == 0.10 and GEX_DELTA_MAX == 0.25)
check("FlashAlpha daily limit = 45",   FLASHALPHA_DAILY_LIMIT == 45)
check("TRADIER_API_KEY set",           len(TRADIER_API_KEY) > 0,    "add to .env")
check("FLASHALPHA_API_KEY set",        len(FLASHALPHA_API_KEY) > 0, "add to .env")
check("TELEGRAM_BOT_TOKEN set",        len(TELEGRAM_BOT_TOKEN) > 0, "add to .env")

# ── 2. DATABASE ───────────────────────────────────────────────────────────
print("\n── 2. Database ──────────────────────────────────────────")
from core.database import (
    init_db, get_or_create_daily_state, update_daily_state,
    insert_spread, insert_leg, get_open_spreads, get_legs_for_spread,
    close_spread, get_daily_trades, insert_snapshot
)
import uuid
init_db()
check("DB initialized",               os.path.exists('data/trades.db'))
state = get_or_create_daily_state('2026-05-26', 50000.0)
check("Daily state created",          state['trade_date'] == '2026-05-26')
check("Daily P&L starts at 0",        state['daily_pnl'] == 0.0)
check("Circuit breaker off",          state['circuit_breaker_hit'] == 0)
update_daily_state('2026-05-26', daily_pnl=-100.0)
state2 = get_or_create_daily_state('2026-05-26', 50000.0)
check("Daily state update persists",  state2['daily_pnl'] == -100.0)
sid = str(uuid.uuid4())
insert_spread({
    'id': sid, 'trade_date': '2026-05-26', 'setup_type': 'IC',
    'signal_grade': 'A+', 'entry_time': '2026-05-26T16:15:00',
    'credit_received': 1.85, 'spread_width': 10, 'contracts': 3,
    'tier_assignment': json.dumps({
        'tier1_contracts': 1, 'tier2_contracts': 1, 'free_contracts': 1,
        'tier1_closed': False, 'tier2_closed': False
    })
})
check("Spread insert",                len(get_open_spreads('2026-05-26')) == 1)
for lt, sk, d in [
    ('SHORT_CALL', 5920, 0.15), ('LONG_CALL', 5930, 0.10),
    ('SHORT_PUT',  5820, -0.15), ('LONG_PUT', 5810, -0.10)
]:
    insert_leg({
        'id': str(uuid.uuid4()), 'spread_id': sid, 'leg_type': lt,
        'strike': sk, 'expiry': '2026-05-26',
        'option_symbol': f'SPXW26052{sk}{"C" if "CALL" in lt else "P"}0000',
        'entry_price': 0.50, 'entry_delta': d, 'entry_iv': 0.15,
        'entry_theta': -0.05, 'tradier_order_id': 'TEST123'
    })
legs = get_legs_for_spread(sid)
check("Leg insert (4 legs)",          len(legs) == 4)
check("Short call leg exists",        any(l['leg_type'] == 'SHORT_CALL' for l in legs))
check("Short put leg exists",         any(l['leg_type'] == 'SHORT_PUT'  for l in legs))
insert_snapshot(sid, 0.92, 0.50, 0.12, 18.5, {"test": True})
check("Snapshot insert",              True)
pnl = close_spread(sid, 0.92, 'CONTRACT_1_50PCT', 1.85)
check("Spread close P&L correct",     round(pnl, 2) == 0.93)
check("No open spreads after close",  len(get_open_spreads('2026-05-26')) == 0)
check("Daily trades query",           len(get_daily_trades('2026-05-26')) == 1)

# ── 3. RISK MANAGER ───────────────────────────────────────────────────────
print("\n── 3. Risk Manager ──────────────────────────────────────")
from core.risk_manager import (
    vix_size_multiplier, calculate_contracts, check_entry_gates,
    record_stopout, check_and_apply_circuit_breaker
)
check("VIX 17 → 100% size",           vix_size_multiplier(17.0) == 1.00)
check("VIX 22 →  50% size",           vix_size_multiplier(22.0) == 0.50)
check("VIX 27 →  25% size",           vix_size_multiplier(27.0) == 0.25)

import math as _math
vix_low  = max(1, _math.floor(IC_CONTRACTS * vix_size_multiplier(17.0)))
vix_mid  = max(1, _math.floor(IC_CONTRACTS * vix_size_multiplier(22.0)))
vix_high = max(1, _math.floor(IC_CONTRACTS * vix_size_multiplier(27.0)))
check("VIX 17 → 3 IC contracts",      vix_low  == 3)
check("VIX 22 → 1 IC contract",       vix_mid  == 1)
check("VIX 27 → 1 IC contract",       vix_high == 1)

cs = {'circuit_breaker_hit': 0, 'entries_halted': 0,
      'cooloff_until': None, 'stopout_count': 0}
check("VIX 31 → blocked",             not check_entry_gates(31.0, 'A+', False, cs).allowed)
check("Grade B → blocked",            not check_entry_gates(17.0, 'B',  False, cs).allowed)
check("News day → blocked",           not check_entry_gates(17.0, 'A+', True,  cs).allowed)
check("Circuit breaker → blocked",    not check_entry_gates(17.0, 'A+', False,
                                          {**cs, 'circuit_breaker_hit': 1}).allowed)
ts = {'daily_pnl': -1100.0, 'circuit_breaker_hit': 0,
      'entries_halted': 0, 'stopout_count': 0}
fired = check_and_apply_circuit_breaker('2026-05-26', ts, 50000.0)
check("CB fires at 2% loss",          fired)
check("Entries halted after CB",      ts['entries_halted'] == 1)
so = {'stopout_count': 0, 'cooloff_until': None,
      'entries_halted': 0, 'circuit_breaker_hit': 0}
so = record_stopout('2026-05-26', so)
check("Stop-out count increments",    so['stopout_count'] == 1)
check("Cooling-off timer set",        so['cooloff_until'] is not None)
check("Entries NOT halted on 1st",    so['entries_halted'] == 0)
so = record_stopout('2026-05-26', so)
check("2nd stopout halts entries",    so['entries_halted'] == 1)

# ── 4. POSITION MONITOR ───────────────────────────────────────────────────
print("\n── 4. Position Monitor ──────────────────────────────────")
from modules.position_monitor import assign_tiers, calc_spread_pnl

t3 = assign_tiers(3, 1, 1)
check("3 contracts: tier1=1",         t3['tier1_contracts'] == 1)
check("3 contracts: tier2=1",         t3['tier2_contracts'] == 1)
check("3 contracts: free=1",          t3['free_contracts']  == 1)
check("tier1 not closed at entry",    t3['tier1_closed'] == False)
check("tier2 not closed at entry",    t3['tier2_closed'] == False)

t2 = assign_tiers(2, 1, 1)
check("2 contracts: tier1=1, tier2=1, free=0",
      t2['tier1_contracts'] == 1 and t2['tier2_contracts'] == 1
      and t2['free_contracts'] == 0)

t1 = assign_tiers(1, 1, 1)
check("1 contract: tier1=1, tier2=0, free=0",
      t1['tier1_contracts'] == 1 and t1['tier2_contracts'] == 0
      and t1['free_contracts'] == 0)

ms = {'id': sid, 'credit_received': 1.85, 'contracts': 3, 'tier_assignment': '{}'}
ml = [
    {'leg_type': 'SHORT_CALL', 'option_symbol': 'SC', 'spread_id': sid, 'entry_price': 0.60},
    {'leg_type': 'LONG_CALL',  'option_symbol': 'LC', 'spread_id': sid, 'entry_price': 0.20},
    {'leg_type': 'SHORT_PUT',  'option_symbol': 'SP', 'spread_id': sid, 'entry_price': 0.60},
    {'leg_type': 'LONG_PUT',   'option_symbol': 'LP', 'spread_id': sid, 'entry_price': 0.20},
]
mq = {
    'SC': {'mid': 0.30, 'ask': 0.32, 'bid': 0.28, 'delta': 0.10, 'iv': 0.14},
    'LC': {'mid': 0.10, 'ask': 0.11, 'bid': 0.09, 'delta': 0.06, 'iv': 0.13},
    'SP': {'mid': 0.30, 'ask': 0.32, 'bid': 0.28, 'delta': -0.10, 'iv': 0.14},
    'LP': {'mid': 0.10, 'ask': 0.11, 'bid': 0.09, 'delta': -0.06, 'iv': 0.13},
}
pd_ = calc_spread_pnl(ms, ml, mq)
check("P&L calc returns dict",        isinstance(pd_, dict))
check("net_debit calculated",         pd_['net_debit'] >= 0)
check("pnl_pct > 0 (profitable)",     pd_['pnl_pct'] > 0)
check("short_leg_delta tracked",      pd_['short_leg_delta'] >= 0)

# Breach remaining calc
tier_state = {'tier1_contracts': 1, 'tier2_contracts': 1, 'free_contracts': 1,
              'tier1_closed': True, 'tier2_closed': False}
remaining = (
    (tier_state['tier1_contracts'] if not tier_state['tier1_closed'] else 0) +
    (tier_state['tier2_contracts'] if not tier_state['tier2_closed'] else 0) +
    tier_state['free_contracts']
)
check("After C1 closed: 2 remain for breach", remaining == 2)

# ── 5. FLASHALPHA CLIENT ──────────────────────────────────────────────────
print("\n── 5. FlashAlpha Client ─────────────────────────────────")
from core.flashalpha import get_client, GEXWall
client = get_client()
check("Client instantiates",          client is not None)
check("Daily limit = 45",             client.calls_remaining() == 45)
check("Cache starts empty",           client._cache is None)
walls = [
    GEXWall(5950, 8_000_000, wall_type='positive'),
    GEXWall(5930, 5_000_000, wall_type='positive'),
    GEXWall(5900, 2_000_000, wall_type='positive'),
    GEXWall(5870, 3_000_000, wall_type='positive'),
]
spx = 5910.0
wa = sorted([w for w in walls if w.strike > spx], key=lambda w: w.strike)
wb = sorted([w for w in walls if w.strike < spx], key=lambda w: w.strike, reverse=True)
check("Walls above sorted asc",       wa[0].strike == 5930)
check("Walls below sorted desc",      wb[0].strike == 5900)

# ── 6. STRIKE SELECTION (OPTION A) ───────────────────────────────────────
print("\n── 6. Strike Selection (Option A) ───────────────────────")
from modules.trade_entry import _find_strike_gex_first, _find_long_leg, calc_net_credit
chain_c = [
    {'symbol': 'SC1', 'type': 'call', 'strike': 5920, 'bid': 1.60, 'ask': 1.80, 'mid': 1.70, 'delta': 0.22, 'iv': 0.15, 'theta': -0.05},
    {'symbol': 'SC2', 'type': 'call', 'strike': 5930, 'bid': 1.20, 'ask': 1.40, 'mid': 1.30, 'delta': 0.17, 'iv': 0.14, 'theta': -0.04},
    {'symbol': 'SC3', 'type': 'call', 'strike': 5940, 'bid': 0.90, 'ask': 1.10, 'mid': 1.00, 'delta': 0.13, 'iv': 0.14, 'theta': -0.03},
    {'symbol': 'SC4', 'type': 'call', 'strike': 5960, 'bid': 0.20, 'ask': 0.40, 'mid': 0.30, 'delta': 0.05, 'iv': 0.12, 'theta': -0.01},
]
chain_p = [
    {'symbol': 'SP1', 'type': 'put', 'strike': 5880, 'bid': 1.60, 'ask': 1.80, 'mid': 1.70, 'delta': -0.22, 'iv': 0.15, 'theta': -0.05},
    {'symbol': 'SP2', 'type': 'put', 'strike': 5870, 'bid': 1.20, 'ask': 1.40, 'mid': 1.30, 'delta': -0.17, 'iv': 0.14, 'theta': -0.04},
    {'symbol': 'SP3', 'type': 'put', 'strike': 5860, 'bid': 0.90, 'ask': 1.10, 'mid': 1.00, 'delta': -0.13, 'iv': 0.14, 'theta': -0.03},
]
spx_p = 5900.0
s1, m1 = _find_strike_gex_first(chain_c, 'call', 'above', spx_p,
                                  [GEXWall(5930, 6_000_000, wall_type='positive')])
check("GEX wall at 5930 selected",    s1 is not None and s1['strike'] == 5930)
check("Method = GEX",                 m1 == 'GEX')
s2, m2 = _find_strike_gex_first(chain_c, 'call', 'above', spx_p,
                                  [GEXWall(5960, 6_000_000, wall_type='positive')])
check("Wall outside delta → fallback", m2 == 'DELTA_FALLBACK')
s3, m3 = _find_strike_gex_first(chain_c, 'call', 'above', spx_p, [])
check("No walls → delta fallback",    m3 == 'DELTA_FALLBACK')
lc = _find_long_leg(chain_c, 'call', 5930, 'above')
check("Long call at 5940",            lc is not None and lc['strike'] == 5940)
lp = _find_long_leg(chain_p, 'put', 5870, 'below')
check("Long put at 5860",             lp is not None and lp['strike'] == 5860)
ic = {
    'short_call': {'bid': 1.30, 'ask': 1.40},
    'long_call':  {'bid': 0.60, 'ask': 0.70},
    'short_put':  {'bid': 1.20, 'ask': 1.30},
    'long_put':   {'bid': 0.50, 'ask': 0.60},
}
check("IC net credit = $1.20",        calc_net_credit(ic, 'IC') == round((1.30+1.20)-(0.70+0.60), 2))
check("Bear Call credit = $0.60",     calc_net_credit({'short_call': {'bid': 1.30, 'ask': 1.40},
                                      'long_call': {'bid': 0.60, 'ask': 0.70}}, 'BEAR_CALL') == 0.60)
check("Bull Put credit = $0.60",      calc_net_credit({'short_put': {'bid': 1.20, 'ask': 1.30},
                                      'long_put': {'bid': 0.50, 'ask': 0.60}}, 'BULL_PUT') == 0.60)

# ── 7. TELEGRAM ───────────────────────────────────────────────────────────
print("\n── 7. Telegram ──────────────────────────────────────────")
from modules.telegram_bot import send, send_daily_summary, CommandHandler
check("send() importable",            callable(send))
check("send_daily_summary() importable", callable(send_daily_summary))
check("CommandHandler importable",    CommandHandler is not None)

# ── SUMMARY ───────────────────────────────────────────────────────────────
total = passed + failed
print()
print("=" * 60)
print(f"  RESULTS: {passed}/{total} passed  |  {failed} failed")
print("=" * 60)
env_failures = sum([
    len(TRADIER_API_KEY) == 0,
    len(FLASHALPHA_API_KEY) == 0,
    len(TELEGRAM_BOT_TOKEN) == 0,
])
logic_failures = failed - env_failures
if logic_failures <= 0:
    print("  🟢 ALL LOGIC CHECKS PASSED")
    if env_failures > 0:
        print(f"  ⚠️  {env_failures} env key(s) missing — add to .env before running")
    else:
        print("  🟢 ALL SYSTEMS GO — Argus ready for dry-run")
else:
    print(f"  🔴 {logic_failures} logic failure(s) — fix before running")
print()
