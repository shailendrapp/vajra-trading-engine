"""
Module: flashalpha.py
FlashAlpha GEX API client for Argus Trading Engine.

Fetches SPX GEX walls (positive/negative nodes) and gamma flip level.
Used by trade_entry.py for Option A strike selection:
  1. Find positive GEX wall where delta is in range → use it as short strike
  2. No wall in delta range → fall back to pure delta-based selection

Free tier: 50 req/day — Argus caches for 30 min to stay well within limits.
Rate usage: ~8–12 calls/day assuming 2 entries per session.

API reference (from prior integration in 0dte-spx-alerts repo):
  GET /v1/exposure/levels/{symbol}  → call_wall, put_wall, gamma_flip, net_gex
  GET /v1/exposure/gex/{symbol}     → per-strike GEX notional data
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Dict
import requests

from config import (
    FLASHALPHA_API_KEY, FLASHALPHA_BASE_URL, FLASHALPHA_SYMBOL,
    FLASHALPHA_DAILY_LIMIT, FLASHALPHA_CACHE_TTL, GEX_WALL_MIN_SIZE,
)

logger = logging.getLogger(__name__)


@dataclass
class GEXWall:
    strike:    float
    net_gex:   float       # positive = dealer long gamma (resistance)
    call_gex:  float = 0.0
    put_gex:   float = 0.0
    wall_type: str   = "positive"   # "positive" | "negative"


@dataclass
class GEXLevels:
    call_wall:    float         # strongest positive GEX wall above price
    put_wall:     float         # strongest positive GEX wall below price
    gamma_flip:   float         # level where GEX changes sign
    net_gex:      float         # overall market GEX
    positive_walls: List[GEXWall] = field(default_factory=list)   # all +GEX nodes above price
    negative_walls: List[GEXWall] = field(default_factory=list)   # all -GEX nodes
    source:       str   = "FlashAlpha"
    fetched_at:   str   = ""
    calls_used:   int   = 0
    calls_left:   int   = 0


class FlashAlphaClient:
    """
    Singleton-style GEX client with 30-minute cache.
    Falls back to VIX-based EM levels if:
      - API key not configured
      - Daily limit reached
      - API call fails
    """

    def __init__(self):
        self._cache:      Optional[GEXLevels] = None
        self._cache_time: Optional[datetime]  = None
        self._call_count: int = 0
        self._call_reset: datetime = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)   # resets at midnight UTC

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {FLASHALPHA_API_KEY}",
            "Accept":        "application/json",
        }

    def _cache_valid(self) -> bool:
        if not self._cache or not self._cache_time:
            return False
        age = (datetime.utcnow() - self._cache_time).total_seconds()
        return age < FLASHALPHA_CACHE_TTL

    def _reset_daily_count_if_needed(self):
        if datetime.utcnow() >= self._call_reset:
            self._call_count = 0
            self._call_reset = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)

    def _get(self, endpoint: str, params: dict = None,
             retries: int = 2) -> Optional[dict]:
        """Raw GET with retry logic."""
        if not FLASHALPHA_API_KEY:
            logger.warning("FLASHALPHA_API_KEY not set — GEX unavailable")
            return None

        self._reset_daily_count_if_needed()
        if self._call_count >= FLASHALPHA_DAILY_LIMIT:
            logger.warning("FlashAlpha daily limit (%d) reached — using fallback",
                           FLASHALPHA_DAILY_LIMIT)
            return None

        url = f"{FLASHALPHA_BASE_URL}{endpoint}"
        for attempt in range(retries):
            try:
                r = requests.get(url, headers=self._headers,
                                 params=params, timeout=10)
                if r.status_code == 429:
                    logger.warning("FlashAlpha rate limit (429) — using fallback")
                    return None
                r.raise_for_status()
                self._call_count += 1
                logger.debug("FlashAlpha call #%d: %s", self._call_count, endpoint)
                return r.json()
            except requests.RequestException as e:
                logger.warning("FlashAlpha attempt %d failed: %s", attempt + 1, e)
                if attempt < retries - 1:
                    time.sleep(1.5)
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # PRIMARY: get_levels — headline GEX metrics
    # ─────────────────────────────────────────────────────────────────────────

    def get_levels(self, symbol: str = None) -> Optional[dict]:
        """
        GET /v1/exposure/levels/{symbol}
        Returns: call_wall, put_wall, gamma_flip, net_gex
        """
        sym = symbol or FLASHALPHA_SYMBOL
        return self._get(f"/exposure/levels/{sym}")

    # ─────────────────────────────────────────────────────────────────────────
    # SECONDARY: get_gex_strikes — per-strike breakdown
    # ─────────────────────────────────────────────────────────────────────────

    def get_gex_strikes(self, symbol: str = None,
                        expiration: str = None) -> Optional[dict]:
        """
        GET /v1/exposure/gex/{symbol}
        Returns per-strike GEX. Pass today's date for 0DTE filter.
        Free tier: single expiration only.
        """
        sym    = symbol or FLASHALPHA_SYMBOL
        params = {}
        if expiration:
            params["expiration"] = expiration
        return self._get(f"/exposure/gex/{sym}", params=params)

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT for trade_entry.py
    # ─────────────────────────────────────────────────────────────────────────

    def get_gex_context(self, spx_price: float,
                        expiration: str = None) -> Optional[GEXLevels]:
        """
        Returns GEXLevels with all walls sorted by size.
        Cached for FLASHALPHA_CACHE_TTL seconds.

        Args:
            spx_price:  current SPX price (used to classify walls as above/below)
            expiration: today's date as YYYY-MM-DD for 0DTE filter

        Returns:
            GEXLevels or None (if API unavailable — caller falls back to delta)
        """
        if self._cache_valid():
            logger.debug("GEX: cache hit (age<30min), calls_used=%d", self._call_count)
            return self._cache

        # ── Fetch levels (headline data) ────────────────────────────────────
        levels_raw = self.get_levels()
        if not levels_raw:
            logger.warning("GEX levels fetch failed — strike selection will use delta fallback")
            return None

        # ── Parse headline fields ────────────────────────────────────────────
        call_wall  = float(levels_raw.get("call_wall")  or
                           levels_raw.get("top_call_gamma_strike") or 0)
        put_wall   = float(levels_raw.get("put_wall")   or
                           levels_raw.get("top_put_gamma_strike")  or 0)
        gamma_flip = float(levels_raw.get("gamma_flip") or
                           levels_raw.get("gamma_flip_level")      or 0)
        net_gex    = float(levels_raw.get("net_gex")    or
                           levels_raw.get("gex")                   or 0)

        # ── Fetch per-strike GEX for detailed wall list ──────────────────────
        positive_walls: List[GEXWall] = []
        negative_walls: List[GEXWall] = []

        strikes_raw = self.get_gex_strikes(expiration=expiration)
        if strikes_raw:
            strike_list = strikes_raw.get("strikes") or []
            for row in strike_list:
                k        = float(row.get("strike", 0))
                net      = float(row.get("net_gex", 0))
                call_gex = float(row.get("call_gex", 0))
                put_gex  = float(row.get("put_gex", 0))

                if abs(net) < GEX_WALL_MIN_SIZE:
                    continue   # too small to matter

                wall = GEXWall(strike=k, net_gex=net,
                               call_gex=call_gex, put_gex=put_gex)
                if net > 0:
                    wall.wall_type = "positive"
                    positive_walls.append(wall)
                else:
                    wall.wall_type = "negative"
                    negative_walls.append(wall)

        # Sort positive walls: above price by ascending strike,
        #                      below price by descending strike
        walls_above = sorted(
            [w for w in positive_walls if w.strike > spx_price],
            key=lambda w: w.strike
        )
        walls_below = sorted(
            [w for w in positive_walls if w.strike < spx_price],
            key=lambda w: w.strike, reverse=True
        )

        result = GEXLevels(
            call_wall       = call_wall,
            put_wall        = put_wall,
            gamma_flip      = gamma_flip,
            net_gex         = net_gex,
            positive_walls  = walls_above + walls_below,
            negative_walls  = sorted(negative_walls, key=lambda w: abs(w.net_gex), reverse=True),
            fetched_at      = datetime.utcnow().isoformat(),
            calls_used      = self._call_count,
            calls_left      = FLASHALPHA_DAILY_LIMIT - self._call_count,
        )

        self._cache      = result
        self._cache_time = datetime.utcnow()

        logger.info(
            "GEX loaded: call_wall=%.0f put_wall=%.0f gamma_flip=%.0f "
            "net_gex=%.0f positive_walls=%d calls_left=%d",
            call_wall, put_wall, gamma_flip, net_gex,
            len(walls_above), result.calls_left
        )
        return result

    def calls_remaining(self) -> int:
        self._reset_daily_count_if_needed()
        return max(0, FLASHALPHA_DAILY_LIMIT - self._call_count)

    def clear_cache(self):
        self._cache      = None
        self._cache_time = None
        logger.info("GEX cache cleared")


# ── Module-level singleton ────────────────────────────────────────────────────
_client: Optional[FlashAlphaClient] = None

def get_client() -> FlashAlphaClient:
    global _client
    if _client is None:
        _client = FlashAlphaClient()
    return _client
