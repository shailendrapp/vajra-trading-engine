"""
Module: tradier.py
All Tradier sandbox API calls — quotes, Greeks, order placement, order status.
Zero business logic here. Just clean API calls with typed returns.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple
import requests

from config import (
    TRADIER_API_KEY, TRADIER_ACCOUNT_ID,
    TRADIER_PAPER_BASE_URL, TRADIER_LIVE_BASE_URL, TRADING_MODE
)

logger = logging.getLogger(__name__)

BASE_URL = TRADIER_PAPER_BASE_URL if TRADING_MODE == "paper" else TRADIER_LIVE_BASE_URL

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept": "application/json",
}


def _get(endpoint: str, params: dict = None, retries: int = 3) -> Optional[dict]:
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.warning("Tradier GET %s attempt %d failed: %s", endpoint, attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    logger.error("Tradier GET %s failed after %d retries", endpoint, retries)
    return None


def _post(endpoint: str, data: dict, retries: int = 3) -> Optional[dict]:
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, data=data, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.warning("Tradier POST %s attempt %d failed: %s", endpoint, attempt + 1, e)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    logger.error("Tradier POST %s failed after %d retries", endpoint, retries)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# QUOTES & GREEKS
# ─────────────────────────────────────────────────────────────────────────────

def get_option_quote(symbol: str) -> Optional[Dict]:
    """
    Fetch a single option quote including Greeks.
    symbol format: SPXW261205C05900000
    Returns dict with: bid, ask, mid, delta, gamma, theta, iv, last
    """
    data = _get("/markets/quotes", params={"symbols": symbol, "greeks": "true"})
    if not data:
        return None
    try:
        quote = data["quotes"]["quote"]
        if isinstance(quote, list):
            quote = quote[0]
        greeks = quote.get("greeks") or {}
        mid = round((quote.get("bid", 0) + quote.get("ask", 0)) / 2, 2)
        return {
            "symbol":   symbol,
            "bid":      quote.get("bid", 0),
            "ask":      quote.get("ask", 0),
            "mid":      mid,
            "last":     quote.get("last", mid),
            "delta":    greeks.get("delta", 0),
            "gamma":    greeks.get("gamma", 0),
            "theta":    greeks.get("theta", 0),
            "iv":       greeks.get("mid_iv", 0),
            "open_interest": quote.get("open_interest", 0),
        }
    except (KeyError, TypeError) as e:
        logger.error("Error parsing option quote for %s: %s", symbol, e)
        return None


def get_multi_option_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """Batch quote fetch — up to 50 symbols in one call."""
    if not symbols:
        return {}
    sym_str = ",".join(symbols)
    data = _get("/markets/quotes", params={"symbols": sym_str, "greeks": "true"})
    result = {}
    if not data:
        return result
    try:
        quotes = data["quotes"]["quote"]
        if isinstance(quotes, dict):
            quotes = [quotes]
        for q in quotes:
            greeks = q.get("greeks") or {}
            mid = round((q.get("bid", 0) + q.get("ask", 0)) / 2, 2)
            result[q["symbol"]] = {
                "symbol": q["symbol"],
                "bid":    q.get("bid", 0),
                "ask":    q.get("ask", 0),
                "mid":    mid,
                "last":   q.get("last", mid),
                "delta":  greeks.get("delta", 0),
                "gamma":  greeks.get("gamma", 0),
                "theta":  greeks.get("theta", 0),
                "iv":     greeks.get("mid_iv", 0),
            }
    except (KeyError, TypeError) as e:
        logger.error("Error parsing multi-quote response: %s", e)
    return result


def get_vix() -> Optional[float]:
    """Fetch current VIX spot price."""
    data = _get("/markets/quotes", params={"symbols": "VIX"})
    if not data:
        return None
    try:
        quote = data["quotes"]["quote"]
        return float(quote.get("last") or quote.get("close") or 0)
    except (KeyError, TypeError):
        return None


def get_spx_price() -> Optional[float]:
    """
    Fetch current SPX spot price.
    Tries multiple symbol formats — sandbox and live use different ones.
    """
    # Symbol formats to try in order
    symbols = ["$SPX.X", "SPX", ".SPX", "SPXW"]
    for sym in symbols:
        data = _get("/markets/quotes", params={"symbols": sym})
        if not data:
            continue
        try:
            quote = data["quotes"]["quote"]
            if isinstance(quote, list):
                quote = quote[0]
            price = float(quote.get("last") or quote.get("close") or
                          quote.get("prevclose") or 0)
            if price > 0:
                logger.info("SPX price fetched via symbol %s: %.2f", sym, price)
                return price
        except (KeyError, TypeError, IndexError):
            continue

    # Final fallback — use SPY × 10 as approximation
    data = _get("/markets/quotes", params={"symbols": "SPY"})
    if data:
        try:
            quote = data["quotes"]["quote"]
            spy = float(quote.get("last") or quote.get("close") or 0)
            if spy > 0:
                spx = round(spy * 10, 2)
                logger.warning("SPX not available — using SPY×10 approximation: %.2f", spx)
                return spx
        except (KeyError, TypeError):
            pass

    logger.error("Could not fetch SPX price from any source")
    return None


def get_option_chain(expiry: str, option_type: str = "all") -> List[Dict]:
    """
    Get full option chain for SPX on a given expiry.
    Tries $SPX.X first (live), then SPXW (sandbox/0DTE).
    expiry: YYYY-MM-DD
    option_type: 'call' | 'put' | 'all'
    """
    for sym in ["$SPX.X", "SPXW", "SPX"]:
        params = {
            "symbol":     sym,
            "expiration": expiry,
            "greeks":     "true",
        }
        if option_type != "all":
            params["optionType"] = option_type

        data = _get("/markets/options/chains", params=params)
        if data and data.get("options"):
            logger.info("Option chain fetched via symbol %s", sym)
            break
    if not data:
        return []
    try:
        options = data["options"]["option"]
        if isinstance(options, dict):
            options = [options]
        result = []
        for o in options:
            greeks = o.get("greeks") or {}
            mid = round((o.get("bid", 0) + o.get("ask", 0)) / 2, 2)
            result.append({
                "symbol":   o.get("symbol"),
                "strike":   float(o.get("strike", 0)),
                "type":     o.get("option_type"),
                "bid":      o.get("bid", 0),
                "ask":      o.get("ask", 0),
                "mid":      mid,
                "delta":    greeks.get("delta", 0),
                "theta":    greeks.get("theta", 0),
                "iv":       greeks.get("mid_iv", 0),
                "oi":       o.get("open_interest", 0),
            })
        return result
    except (KeyError, TypeError) as e:
        logger.error("Error parsing option chain: %s", e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def place_spread_order(
    legs: List[Dict],
    order_type: str = "market",
    limit_price: Optional[float] = None,
    duration: str = "day",
) -> Optional[Dict]:
    """
    Place a multi-leg spread order on Tradier paper account.

    legs format: [
        {"symbol": "SPXW...", "side": "sell_to_open", "quantity": 1},
        {"symbol": "SPXW...", "side": "buy_to_open",  "quantity": 1},
    ]

    Returns order dict with 'id' and 'status', or None on failure.
    """
    if len(legs) not in (2, 4):
        logger.error("place_spread_order: expected 2 or 4 legs, got %d", len(legs))
        return None

    data = {
        "class":    "multileg",
        "symbol":   "$SPX.X",
        "type":     order_type,
        "duration": duration,
    }

    if order_type == "limit" and limit_price is not None:
        data["price"] = str(round(limit_price, 2))

    for i, leg in enumerate(legs, start=1):
        data[f"option_symbol[{i}]"] = leg["symbol"]
        data[f"side[{i}]"]          = leg["side"]
        data[f"quantity[{i}]"]      = str(leg["quantity"])

    response = _post(f"/accounts/{TRADIER_ACCOUNT_ID}/orders", data)
    if not response:
        return None

    try:
        order = response.get("order", {})
        logger.info("Order placed: id=%s status=%s", order.get("id"), order.get("status"))
        return order
    except (KeyError, TypeError) as e:
        logger.error("Error parsing order response: %s | raw: %s", e, response)
        return None


def place_close_order(
    legs: List[Dict],
    order_type: str = "market",
    limit_price: Optional[float] = None,
    duration: str = "day",
) -> Optional[Dict]:
    """
    Close an existing spread. Same structure as place_spread_order but
    sides are buy_to_close / sell_to_close.
    """
    return place_spread_order(legs, order_type, limit_price, duration)


def get_order_status(order_id: str) -> Optional[Dict]:
    """Poll order status until filled or cancelled."""
    data = _get(f"/accounts/{TRADIER_ACCOUNT_ID}/orders/{order_id}")
    if not data:
        return None
    try:
        return data.get("order", {})
    except (KeyError, TypeError):
        return None


def get_account_balance() -> Optional[Dict]:
    """Returns cash, option_buying_power, total_equity."""
    data = _get(f"/accounts/{TRADIER_ACCOUNT_ID}/balances")
    if not data:
        return None
    try:
        b = data["balances"]
        return {
            "total_equity":          b.get("total_equity", 0),
            "cash":                  b.get("cash", {}).get("cash_available", 0),
            "option_buying_power":   b.get("option_short_value", 0),
        }
    except (KeyError, TypeError) as e:
        logger.error("Error parsing account balance: %s", e)
        return None
