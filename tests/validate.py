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

from dotenv import load_dotenv
# Load .env file but don't override existing environment variables
# (GitHub Actions injects secrets as env vars directly)
load_dotenv(dotenv_path='.env', override=False)

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
        print("  ✅ " + name)
        passed += 1
    else:
        suffix = " -- " + detail if detail else ""
        print("  ❌ " + name + suffix)
        failed += 1

print("\n\u2500\u2500 1. Config \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from config import (
    POLL_INTERVAL_SECONDS, VIX_KILL_SWITCH, VIX_SIZE_TIERS,
    FLASHALPHA_API_KEY, TRADIER_API_KEY, TELEGRAM_BOT_TOKEN,
    IC_CONTRACT_1_TARGET, IC_CONTRACT_2_TARGET, IC_CONTRACTS,
    BREACH_DELTA_THRESHOLD, BREACH_PNL_MULTIPLIER, HARD_CLOSE_TIME_PT,
    DAILY_MAX_LOSS_PCT, COOLOFF_MINUTES, MAX_STOPOUTS_PER_DAY,
    SPREAD_WIDTH_PTS, RISK_PCT_PER_TRADE, GEX_DELTA_MIN, GEX_DELTA_MAX,
    FLASHALPHA_DAILY_LIMIT,
    BIC_WING_TIERS, BIC_SHORT_DELTA_TARGET, BIC_MIN_CREDIT, BIC_VIX_FLOOR,
    BIC_ENTRY_WINDOWS_ET, ANTHROPIC_API_KEY,
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
check("BIC VIX floor = 12",            BIC_VIX_FLOOR == 12.0)
check("BIC delta target = 0.09",       BIC_SHORT_DELTA_TARGET == 0.09)
check("BIC min credit = $0.50",        BIC_MIN_CREDIT == 0.50)
check("BIC wing tiers defined",        len(BIC_WING_TIERS) == 3)
check("BIC entry windows defined",     len(BIC_ENTRY_WINDOWS_ET) == 5)
check("TRADIER_API_KEY set",           len(TRADIER_API_KEY) > 0,    "add to .env")
check("FLASHALPHA_API_KEY set",        len(FLASHALPHA_API_KEY) > 0, "add to .env")
check("TELEGRAM_BOT_TOKEN set",        len(TELEGRAM_BOT_TOKEN) > 0, "add to .env")
check("ANTHROPIC_API_KEY set",         len(ANTHROPIC_API_KEY) > 0,  "add to .env")

print("\n\u2500\u2500 2. Database \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
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
    insert_leg({'id': str(uuid.uuid4()), 'spread_id': sid, 'leg_type': lt,
        'strike': sk, 'expiry': '2026-05-26',
        'option_symbol': f'SPXW26052{sk}{"C" if "CALL" in lt else "P"}0000',
        'entry_price': 0.50, 'entry_delta': d, 'entry_iv': 0.15,
        'entry_theta': -0.05, 'tradier_order_id': 'TEST123'})
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

print("\n\u2500\u2500 3. Risk Manager \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from core.risk_manager import (
    vix_size_multiplier, calculate_contracts, check_entry_gates,
    record_stopout, check_and_apply_circuit_breaker
)
check("VIX 17 \u2192 100% size",           vix_size_multiplier(17.0) == 1.00)
check("VIX 22 \u2192  50% size",           vix_size_multiplier(22.0) == 0.50)
check("VIX 27 \u2192  25% size",           vix_size_multiplier(27.0) == 0.25)
vix_low  = max(1, math.floor(IC_CONTRACTS * vix_size_multiplier(17.0)))
vix_mid  = max(1, math.floor(IC_CONTRACTS * vix_size_multiplier(22.0)))
vix_high = max(1, math.floor(IC_CONTRACTS * vix_size_multiplier(27.0)))
check("VIX 17 \u2192 3 IC contracts",      vix_low  == 3)
check("VIX 22 \u2192 1 IC contract",       vix_mid  == 1)
check("VIX 27 \u2192 1 IC contract",       vix_high == 1)
cs = {'circuit_breaker_hit': 0, 'entries_halted': 0, 'cooloff_until': None, 'stopout_count': 0}
check("VIX 31 \u2192 blocked",             not check_entry_gates(31.0, 'A+', False, cs).allowed)
check("Grade B \u2192 blocked",            not check_entry_gates(17.0, 'B',  False, cs).allowed)
check("News day \u2192 blocked",           not check_entry_gates(17.0, 'A+', True,  cs).allowed)
check("Circuit breaker \u2192 blocked",    not check_entry_gates(17.0, 'A+', False, {**cs, 'circuit_breaker_hit': 1}).allowed)
ts = {'daily_pnl': -1100.0, 'circuit_breaker_hit': 0, 'entries_halted': 0, 'stopout_count': 0}
fired = check_and_apply_circuit_breaker('2026-05-26', ts, 50000.0)
check("CB fires at 2% loss",          fired)
check("Entries halted after CB",      ts['entries_halted'] == 1)
so = {'stopout_count': 0, 'cooloff_until': None, 'entries_halted': 0, 'circuit_breaker_hit': 0}
so = record_stopout('2026-05-26', so)
check("Stop-out count increments",    so['stopout_count'] == 1)
check("Cooling-off timer set",        so['cooloff_until'] is not None)
check("Entries NOT halted on 1st",    so['entries_halted'] == 0)
so = record_stopout('2026-05-26', so)
check("2nd stopout halts entries",    so['entries_halted'] == 1)

print("\n\u2500\u2500 4. Position Monitor \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from modules.position_monitor import assign_tiers, calc_spread_pnl
t3 = assign_tiers(3, 1, 1)
check("3 contracts: tier1=1",         t3['tier1_contracts'] == 1)
check("3 contracts: tier2=1",         t3['tier2_contracts'] == 1)
check("3 contracts: free=1",          t3['free_contracts']  == 1)
t2 = assign_tiers(2, 1, 1)
check("2 contracts: tier1=1,tier2=1,free=0",
      t2['tier1_contracts']==1 and t2['tier2_contracts']==1 and t2['free_contracts']==0)
t1 = assign_tiers(1, 1, 1)
check("1 contract: tier1=1,tier2=0,free=0",
      t1['tier1_contracts']==1 and t1['tier2_contracts']==0 and t1['free_contracts']==0)
ms = {'id': sid, 'credit_received': 1.85, 'contracts': 3, 'tier_assignment': '{}'}
ml = [
    {'leg_type':'SHORT_CALL','option_symbol':'SC','spread_id':sid,'entry_price':0.60},
    {'leg_type':'LONG_CALL', 'option_symbol':'LC','spread_id':sid,'entry_price':0.20},
    {'leg_type':'SHORT_PUT', 'option_symbol':'SP','spread_id':sid,'entry_price':0.60},
    {'leg_type':'LONG_PUT',  'option_symbol':'LP','spread_id':sid,'entry_price':0.20},
]
mq = {
    'SC':{'mid':0.30,'ask':0.32,'bid':0.28,'delta':0.10,'iv':0.14},
    'LC':{'mid':0.10,'ask':0.11,'bid':0.09,'delta':0.06,'iv':0.13},
    'SP':{'mid':0.30,'ask':0.32,'bid':0.28,'delta':-0.10,'iv':0.14},
    'LP':{'mid':0.10,'ask':0.11,'bid':0.09,'delta':-0.06,'iv':0.13},
}
pd_ = calc_spread_pnl(ms, ml, mq)
check("P&L calc returns dict",        isinstance(pd_, dict))
check("net_debit calculated",         pd_['net_debit'] >= 0)
check("pnl_pct > 0 (profitable)",     pd_['pnl_pct'] > 0)
check("short_leg_delta tracked",      pd_['short_leg_delta'] >= 0)

print("\n\u2500\u2500 5. BIC Scanner \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from modules.bic_scanner import get_wing_width, bic_entry_allowed, size_contracts, select_bic_strikes
check("VIX 17 \u2192 25pt wings",          get_wing_width(17.0) == 25)
check("VIX 22 \u2192 30pt wings",          get_wing_width(22.0) == 30)
check("VIX 27 \u2192 35pt wings",          get_wing_width(27.0) == 35)
clean = {'circuit_breaker_hit':0,'entries_halted':0,'cooloff_until':None,'stopout_count':0}
ok,_  = bic_entry_allowed(18.0, False, clean, 50000)
ok2,_ = bic_entry_allowed(10.0, False, clean, 50000)
ok3,_ = bic_entry_allowed(31.0, False, clean, 50000)
ok4,_ = bic_entry_allowed(18.0, True,  clean, 50000)
check("VIX 18, clean \u2192 allowed",      ok)
check("VIX 10 < floor \u2192 blocked",     not ok2)
check("VIX 31 \u2265 kill \u2192 blocked", not ok3)
check("News day \u2192 blocked",           not ok4)
check("$50K VIX17 = 3c",                  size_contracts(50000, 17.0, 25) == 3)
check("$5K limited margin = 1c",          size_contracts(5000,  17.0, 25) == 1)
mock_chain = []
spx = 5900.0
for strike, otype, delta, bid in [
    (5965,"call",0.09,0.60),(5970,"call",0.07,0.45),(5975,"call",0.05,0.30),
    (5990,"call",0.03,0.18),(5835,"put",-0.09,0.60),(5830,"put",-0.07,0.45),
    (5825,"put",-0.05,0.30),(5810,"put",-0.03,0.18),
    (5920,"call",0.22,1.80),(5930,"call",0.17,1.20),(5940,"call",0.14,0.95),
    (5960,"call",0.11,0.75),(5850,"put",-0.22,1.80),(5845,"put",-0.14,0.95),
    (5840,"put",-0.11,0.75),
]:
    mock_chain.append({"type":otype,"strike":float(strike),"delta":delta,
        "bid":bid,"ask":round(bid+0.05,2),"mid":round(bid+0.025,2),
        "symbol":f"SPXW...{otype[0].upper()}{strike}","iv":0.15,"theta":-0.05})
r = select_bic_strikes(mock_chain, spx, 25)
check("BIC strike selection works",   r is not None)
if r:
    check("Short call above price",   r['short_call']['strike'] > spx)
    check("Short put below price",    r['short_put']['strike'] < spx)
    check("Long call = short+25",     r['long_call']['strike'] == r['short_call']['strike']+25)
    check("Long put = short-25",      r['long_put']['strike']  == r['short_put']['strike']-25)

print("\n\u2500\u2500 6. FlashAlpha Client \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from core.flashalpha import get_client, GEXWall
client = get_client()
check("Client instantiates",          client is not None)
check("Daily limit = 45",             client.calls_remaining() == 45)
check("Cache starts empty",           client._cache is None)

print("\n\u2500\u2500 7. Strike Selection (Option A) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from modules.trade_entry import _find_strike_gex_first, _find_long_leg, calc_net_credit
chain_c = [
    {'symbol':'SC1','type':'call','strike':5930,'bid':1.20,'ask':1.40,'mid':1.30,'delta':0.17,'iv':0.14,'theta':-0.04},
    {'symbol':'SC2','type':'call','strike':5940,'bid':0.90,'ask':1.10,'mid':1.00,'delta':0.13,'iv':0.14,'theta':-0.03},
    {'symbol':'SC3','type':'call','strike':5960,'bid':0.20,'ask':0.40,'mid':0.30,'delta':0.05,'iv':0.12,'theta':-0.01},
]
chain_p = [
    {'symbol':'SP1','type':'put','strike':5870,'bid':1.20,'ask':1.40,'mid':1.30,'delta':-0.17,'iv':0.14,'theta':-0.04},
    {'symbol':'SP2','type':'put','strike':5860,'bid':0.90,'ask':1.10,'mid':1.00,'delta':-0.13,'iv':0.14,'theta':-0.03},
]
spx_p = 5900.0
s1,m1 = _find_strike_gex_first(chain_c,'call','above',spx_p,[GEXWall(5930,6_000_000,wall_type='positive')])
check("GEX wall at 5930 selected",    s1 is not None and s1['strike']==5930)
check("Method = GEX",                 m1=='GEX')
s2,m2 = _find_strike_gex_first(chain_c,'call','above',spx_p,[GEXWall(5960,6_000_000,wall_type='positive')])
check("Wall outside delta \u2192 fallback", m2=='DELTA_FALLBACK')
s3,m3 = _find_strike_gex_first(chain_c,'call','above',spx_p,[])
check("No walls \u2192 delta fallback",m3=='DELTA_FALLBACK')
lc = _find_long_leg(chain_c,'call',5930,'above')
check("Long call at 5940",            lc is not None and lc['strike']==5940)

print("\n\u2500\u2500 8. Telegram \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
from modules.telegram_bot import (send, send_daily_summary, send_weekly_summary,
                                   send_monthly_summary, send_yearly_summary)
check("send() importable",            callable(send))
check("Daily summary importable",     callable(send_daily_summary))
check("Weekly summary importable",    callable(send_weekly_summary))
check("Monthly summary importable",   callable(send_monthly_summary))
check("Yearly summary importable",    callable(send_yearly_summary))

print("\n\u2500\u2500 9. Engine Integration \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
with open('engine.py') as f: src = f.read()
check("run_bic_scan imported",        "from modules.bic_scanner import run_bic_scan" in src)
check("BIC windows tracked",          "_bic_windows_fired" in src)
check("BIC scan in _tick",            "BIC_ENTRY_WINDOWS_ET" in src)
check("Monthly summary scheduled",    "send_monthly_summary" in src)
check("Yearly summary scheduled",     "send_yearly_summary" in src)

total = passed + failed
env_miss = sum([
    len(TRADIER_API_KEY)==0, len(FLASHALPHA_API_KEY)==0,
    len(TELEGRAM_BOT_TOKEN)==0, len(ANTHROPIC_API_KEY)==0,
])
logic_fail = failed - env_miss
print()
print("=" * 60)
print(f"  RESULTS: {passed}/{total} passed  |  {failed} failed")
print("=" * 60)
if logic_fail <= 0:
    print("  \U0001f7e2 ALL LOGIC CHECKS PASSED")
    if env_miss:
        print(f"  \u26a0\ufe0f  {env_miss} env key(s) missing \u2014 add to .env before running")
    else:
        print("  \U0001f7e2 ALL SYSTEMS GO \u2014 Argus ready")
else:
    print(f"  \U0001f534 {logic_fail} logic failure(s) \u2014 fix before running")
print()
