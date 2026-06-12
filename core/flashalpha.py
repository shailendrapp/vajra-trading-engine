"""
Module: flashalpha.py
FlashAlpha API client — SPX GEX, Expected Move, and Strike Selection

Uses /v1/stock/spx/summary endpoint (free tier, no rate limit issues)
Returns all data needed for BIC strike selection in one API call:
  - gamma_flip, call_wall, put_wall, regime
  - atm_iv (for expected move calculation)
  - vix level
  - net_gex (positive/negative gamma regime)

Free tier: unlimited calls to /stock/{symbol}/summary
Auth: X-Api-Key header
Base URL: https://lab.flashalpha.com/v1
"""

import logging
import math
import time
from typing import Optional, Dict, List
from dataclasses import dataclass
import requests

from config import FLASHALPHA_API_KEY, FLASHALPHA_BASE_URL

logger = logging.getLogger(__name__)

SUMMARY_ENDPOINT = "/stock/spx/summary"
REQUEST_TIMEOUT  = 10
MAX_RETRIES      = 2


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GEXWall:
    strike:    float
    size:      float
    wall_type: str   # 'call_wall' | 'put_wall' | 'gamma_flip'


@dataclass
class SPXSummary:
    """Complete SPX market context from FlashAlpha."""
    spx_price:      float
    atm_iv:         float    # e.g. 0.1132 (11.32%)
    vix:            float
    gamma_flip:     float    # SPX level where dealers flip short gamma
    call_wall:      float    # strongest call GEX wall (resistance)
    put_wall:       float    # strongest put GEX wall (support)
    net_gex:        float    # positive = long gamma = range bound
    regime:         str      # 'positive_gamma' | 'negative_gamma'
    expected_move:  float    # 1-day 1-sigma move in SPX points
    wing_width:     int      # recommended wing width from expected move
    go_signal:      bool     # True if regime favors IC trading
    vvix:           float = 0.0   # VIX of VIX — >100 = elevated spike risk


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────

DISK_CACHE_PATH    = "data/flashalpha_cache.json"
DISK_CACHE_TTL     = 3300   # 55 min disk cache — one call per 60-min BIC window
MIN_FORCE_INTERVAL = 600    # 10 min minimum between forced refreshes
                             # prevents burst when all windows fire simultaneously


class FlashAlphaClient:

    def __init__(self):
        self._headers = {
            "X-Api-Key": FLASHALPHA_API_KEY,
            "Accept":    "application/json",
        }
        self._cache:      Optional[SPXSummary] = None
        self._cache_time: float = 0
        self._cache_ttl:  int   = 1800   # in-memory 30 min cache
        self._load_disk_cache()          # restore from previous run if fresh

    def calls_remaining(self) -> int:
        """Compatibility shim for validate.py."""
        return 45

    def _load_disk_cache(self):
        """Load cached FlashAlpha response from disk if still fresh."""
        import json, os
        try:
            if not os.path.exists(DISK_CACHE_PATH):
                return
            with open(DISK_CACHE_PATH, "r") as f:
                data = json.load(f)
            age = time.time() - data.get("saved_at", 0)
            if age > DISK_CACHE_TTL:
                logger.debug("Disk cache expired (%.0f min old)", age/60)
                return
            # Reconstruct SPXSummary from cached dict
            s = data["summary"]
            self._cache = SPXSummary(
                spx_price=s["spx_price"], atm_iv=s["atm_iv"],
                vix=s["vix"], gamma_flip=s["gamma_flip"],
                call_wall=s["call_wall"], put_wall=s["put_wall"],
                net_gex=s["net_gex"], regime=s["regime"],
                expected_move=s["expected_move"], wing_width=s["wing_width"],
                go_signal=s["go_signal"], vvix=s.get("vvix", 0.0)
            )
            self._cache_time = data["saved_at"]
            logger.info(
                "FlashAlpha: restored from disk cache (%.0f min old) "
                "regime=%s em=±%.0f",
                age/60, self._cache.regime, self._cache.expected_move
            )
        except Exception as e:
            logger.debug("Disk cache load failed: %s", e)

    def _save_disk_cache(self, summary: "SPXSummary"):
        """Save FlashAlpha response to disk for use by next run."""
        import json, os
        try:
            os.makedirs("data", exist_ok=True)
            data = {
                "saved_at": time.time(),
                "summary": {
                    "spx_price":    summary.spx_price,
                    "atm_iv":       summary.atm_iv,
                    "vix":          summary.vix,
                    "gamma_flip":   summary.gamma_flip,
                    "call_wall":    summary.call_wall,
                    "put_wall":     summary.put_wall,
                    "net_gex":      summary.net_gex,
                    "regime":       summary.regime,
                    "expected_move":summary.expected_move,
                    "wing_width":   summary.wing_width,
                    "go_signal":    summary.go_signal,
                    "vvix":         summary.vvix,
                }
            }
            with open(DISK_CACHE_PATH, "w") as f:
                json.dump(data, f)
            logger.debug("FlashAlpha: saved to disk cache")
        except Exception as e:
            logger.warning("Disk cache save failed: %s", e)

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        url = f"{FLASHALPHA_BASE_URL}{endpoint}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.get(
                    url, headers=self._headers,
                    params=params, timeout=REQUEST_TIMEOUT
                )
                if r.ok:
                    return r.json()
                logger.warning(
                    "FlashAlpha attempt %d failed: %s for url: %s",
                    attempt, r.status_code, url
                )
            except requests.RequestException as e:
                logger.warning("FlashAlpha attempt %d error: %s", attempt, e)
            if attempt < MAX_RETRIES:
                time.sleep(2)
        return None

    def _is_cache_from_today_session(self) -> bool:
        """
        Returns True if cached data is from the current trading session
        (after 9:00 AM ET today). Prevents stale overnight data being
        used for intraday decisions.
        """
        if not self._cache_time:
            return False
        try:
            import datetime
            import pytz
            ET = pytz.timezone("America/New_York")
            now_et   = datetime.datetime.now(ET)
            cache_et = datetime.datetime.fromtimestamp(self._cache_time, ET)
            # Session starts at 9:00 AM ET
            session_start = now_et.replace(hour=9, minute=0, second=0, microsecond=0)
            return cache_et >= session_start
        except Exception:
            return False

    def get_spx_summary(self, force: bool = False) -> Optional[SPXSummary]:
        """
        Fetch full SPX summary from FlashAlpha.

        force=True: called once per BIC window (10:15, 11:15, 12:15, 1:15, 2:15 ET)
                    to ensure GEX walls are fresh for each entry decision.
                    Subject to MIN_FORCE_INTERVAL (10 min) to prevent burst
                    when all windows fire simultaneously on a late start.

        force=False: used by position monitor (every 45s) — uses cached data
                     from most recent BIC window scan.

        5 BIC windows × 1 forced call each = 5 calls/day = free tier limit exactly.
        """
        now = time.time()

        # If force=True but last fetch was < MIN_FORCE_INTERVAL ago,
        # downgrade to cache — prevents burst on simultaneous window fires
        if force and self._cache and (now - self._cache_time) < MIN_FORCE_INTERVAL:
            logger.debug(
                "FlashAlpha: force refresh skipped — last fetch %.0f min ago (min=%.0f)",
                (now - self._cache_time) / 60, MIN_FORCE_INTERVAL / 60
            )
            force = False

        # For non-forced requests: use cache if fresh AND from today's session
        if not force and self._cache and self._is_cache_from_today_session():
            age = now - self._cache_time
            if age < self._cache_ttl:
                logger.debug(
                    "FlashAlpha: using cached summary (%.0f min old)", age / 60
                )
                return self._cache

        # Cache is stale, from yesterday, or force=True — fetch fresh
        if self._cache and not force:
            if not self._is_cache_from_today_session():
                logger.info(
                    "FlashAlpha: disk cache is from previous session — fetching fresh"
                )
            else:
                logger.debug("FlashAlpha: cache expired — fetching fresh")

        data = self._get(SUMMARY_ENDPOINT)
        if not data:
            logger.warning("FlashAlpha summary unavailable — BIC will use delta+VIX fallback")
            return None

        try:
            spx_price  = float(data["price"]["last"])
            atm_iv     = float(data["volatility"]["atm_iv"]) / 100
            vix        = float(data["macro"]["vix"]["value"])
            exposure   = data.get("exposure", {})
            gamma_flip = float(exposure.get("gamma_flip", 0))
            call_wall  = float(exposure.get("call_wall", 0))
            put_wall   = float(exposure.get("put_wall", 0))
            net_gex    = float(exposure.get("net_gex", 0))
            regime     = exposure.get("regime", "unknown")

            # Expected move: SPX × IV × sqrt(1/252) = 1-day 1-sigma
            expected_move = round(spx_price * atm_iv * math.sqrt(1 / 252), 1)

            # Wing width = max of VIX-based tier and 60% of expected move
            # 60% ensures short strikes are outside the expected move
            em_wing    = max(25, round(expected_move * 0.60 / 5) * 5)
            vix_wing   = self._vix_wing(vix)
            wing_width = max(em_wing, vix_wing)

            # GO signal: positive gamma + net GEX positive = ideal IC conditions
            go_signal  = regime == "positive_gamma" and net_gex > 0

            # VVIX — extract before SPXSummary constructor
            vvix_val = 0.0
            try:
                vvix_val = float(
                    data.get("macro", {}).get("vvix", {}).get("value", 0) or 0
                )
            except (TypeError, ValueError):
                vvix_val = 0.0

            summary = SPXSummary(
                spx_price     = spx_price,
                atm_iv        = atm_iv,
                vix           = vix,
                gamma_flip    = gamma_flip,
                call_wall     = call_wall,
                put_wall      = put_wall,
                net_gex       = net_gex,
                regime        = regime,
                expected_move = expected_move,
                wing_width    = wing_width,
                go_signal     = go_signal,
                vvix          = vvix_val,
            )

            self._cache      = summary
            self._cache_time = now
            self._save_disk_cache(summary)   # persist for next run

            # (vvix_val already extracted above)
            # except block placeholder:
            if False:
                vvix_val = 0.0

            logger.info(
                "FlashAlpha SPX: price=%.2f iv=%.1f%% vix=%.1f "
                "flip=%.0f call_wall=%.0f put_wall=%.0f "
                "em=±%.0f wing=%dpt regime=%s go=%s vvix=%.0f",
                spx_price, atm_iv*100, vix,
                gamma_flip, call_wall, put_wall,
                expected_move, wing_width, regime, go_signal, vvix_val
            )
            return summary

        except (KeyError, TypeError, ValueError) as e:
            logger.error("FlashAlpha parse error: %s | raw: %s", e, str(data)[:200])
            return None

    def _vix_wing(self, vix: float) -> int:
        """VIX-based wing width tier."""
        from config import BIC_WING_TIERS
        for threshold, width in BIC_WING_TIERS:
            if vix < threshold:
                return width
        return BIC_WING_TIERS[-1][1]

    def get_strike_anchors(
        self,
        spx_price: float,
        summary:   Optional[SPXSummary] = None
    ) -> Dict:
        """
        Returns recommended strike placement based on GEX walls.

        Rules:
          - Short call: above call_wall AND above (spx + expected_move × 0.8)
          - Short put:  below put_wall AND below (spx - expected_move × 0.8)
          - Snap both to nearest 5pt increment

        Falls back to delta-based placement if no GEX data.
        """
        if summary is None:
            summary = self.get_spx_summary()

        if summary and summary.call_wall > 0 and summary.put_wall > 0:
            # GEX-anchored placement
            min_call = spx_price + summary.expected_move * 0.80
            min_put  = spx_price - summary.expected_move * 0.80

            short_call = max(summary.call_wall + 5, min_call)
            short_put  = min(summary.put_wall  - 5, min_put)

            # Snap to 5pt grid
            short_call = math.ceil(short_call / 5) * 5
            short_put  = math.floor(short_put  / 5) * 5

            logger.info(
                "GEX anchors: short_call=%.0f (wall=%.0f+5 vs EM=%.0f) "
                "short_put=%.0f (wall=%.0f-5 vs EM=%.0f)",
                short_call, summary.call_wall, min_call,
                short_put,  summary.put_wall,  min_put
            )
            return {
                "short_call": short_call,
                "short_put":  short_put,
                "method":     "GEX",
                "wing_width": summary.wing_width,
                "em":         summary.expected_move,
            }

        # Fallback: BS-based placement at delta 0.09
        logger.warning("GEX unavailable — using BS delta fallback for anchors")
        return {"method": "DELTA_FALLBACK", "wing_width": 25}


# ─────────────────────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────────────────────

_client: Optional[FlashAlphaClient] = None


def get_client() -> FlashAlphaClient:
    global _client
    if _client is None:
        _client = FlashAlphaClient()
    return _client


def get_spx_summary() -> Optional[SPXSummary]:
    """Convenience function for bic_scanner.py."""
    return get_client().get_spx_summary()
