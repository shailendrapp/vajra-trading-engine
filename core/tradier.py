"""
Module: tradier.py
Tradier Brokerage API wrapper — built from official docs at docs.tradier.com

Key findings from official documentation:
  - Multileg orders: class=multileg, symbol=underlying (e.g. "SPX"), legs indexed [0]
  - SPX quotes: use "$SPX.X" for index price, "SPXW" for 0DTE option chains
  - Option chains: symbol="SPXW", expiration=YYYY-MM-DD
  - All requests: Content-Type: application/x-www-form-urlencoded
  - Auth: Authorization: Bearer <token>
  - Sandbox: https://sandbox.tradier.com/v1
"""

import logging
import time
from typing import Dict, List, Optional
import requests

from config import (
    TRADIER_API_KEY, TRADIER_ACCOUNT_ID,
    TRADIER_PAPER_BASE_URL, TRADIER_LIVE_BASE_URL, TRADING_MODE
)

logger = logging.getLogger(__name__)

BASE_URL = TRADIER_PAPER_BASE_URL if TRADING_MODE == "paper" else TRADIER_LIVE_BASE_URL

HEADERS = {
    "Authorization": f"Bearer {TRADIER_API_KEY}",
    "Accept":        "application/json",
    "Content-Type":  "application/x-www-form-urlencoded",
}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

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
            if not r.ok:
                logger.warning(
                    "Tradier POST %s attempt %d failed: %s for url: %s | body: %s",
                    endpoint, attempt + 1, r.status_code, url, r.text[:800]
                )
                if attempt < retries - 1:
                    time.sleep(1.5 ** attempt)
                continue
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
    """Fetch a single option quote including Greeks."""
    data = _get("/markets/quotes", params={"symbols": symbol, "greeks": "true"})
    if not data:
        return None
    try:
        quote = data["quotes"]["quote"]
        if isinstance(quote, list):
            quote = quote[0]
        greeks = quote.get("greeks") or {}
        mid    = round((quote.get("bid", 0) + quote.get("ask", 0)) / 2, 2)
        return {
            "symbol": symbol,
            "bid":    quote.get("bid", 0),
            "ask":    quote.get("ask", 0),
            "mid":    mid,
            "last":   quote.get("last", mid),
            "delta":  greeks.get("delta", 0),
            "gamma":  greeks.get("gamma", 0),
            "theta":  greeks.get("theta", 0),
            "iv":     greeks.get("mid_iv", 0),
        }
    except (KeyError, TypeError) as e:
        logger.error("Error parsing option quote for %s: %s", symbol, e)
        return None


def get_multi_option_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """Batch quote fetch — up to 50 symbols in one call."""
    if not symbols:
        return {}
    data = _get("/markets/quotes", params={"symbols": ",".join(symbols), "greeks": "true"})
    result = {}
    if not data:
        return result
    try:
        quotes = data["quotes"]["quote"]
        if isinstance(quotes, dict):
            quotes = [quotes]
        for q in quotes:
            greeks = q.get("greeks") or {}
            mid    = round((q.get("bid", 0) + q.get("ask", 0)) / 2, 2)
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
        if isinstance(quote, list):
            quote = quote[0]
        return float(quote.get("last") or quote.get("close") or 0)
    except (KeyError, TypeError):
        return None


def get_spx_price() -> Optional[float]:
    """
    Fetch current SPX spot price.
    Tries $SPX.X first (standard index symbol), then SPY×10 as fallback.
    """
    # Try index symbols in order
    for sym in ["$SPX.X", "SPX", ".SPX"]:
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

    # Final fallback: SPY × 10
    data = _get("/markets/quotes", params={"symbols": "SPY"})
    if data:
        try:
            quote = data["quotes"]["quote"]
            if isinstance(quote, list):
                quote = quote[0]
            spy = float(quote.get("last") or quote.get("close") or 0)
            if spy > 0:
                spx = round(spy * 10, 2)
                logger.warning("SPX unavailable — using SPY×10 approximation: %.2f", spx)
                return spx
        except (KeyError, TypeError):
            pass

    logger.error("Could not fetch SPX price from any source")
    return None


def get_option_chain(expiry: str, option_type: str = "all") -> List[Dict]:
    """
    Get full SPX option chain for a given expiry.
    Uses SPXW for 0DTE options (weekly/daily expirations).
    Per Tradier docs: GET /v1/markets/options/chains?symbol=SPXW&expiration=YYYY-MM-DD
    """
    for sym in ["SPXW", "$SPX.X", "SPX"]:
        params = {
            "symbol":     sym,
            "expiration": expiry,
            "greeks":     "true",
        }
        if option_type != "all":
            params["optionType"] = option_type

        data = _get("/markets/options/chains", params=params)
        if data and data.get("options") and data["options"].get("option"):
            logger.info("Option chain fetched via symbol %s", sym)
            options = data["options"]["option"]
            if isinstance(options, dict):
                options = [options]
            result = []
            for o in options:
                greeks = o.get("greeks") or {}
                bid    = float(o.get("bid") or 0)
                ask    = float(o.get("ask") or 0)
                mid    = round((bid + ask) / 2, 2)
                result.append({
                    "symbol":  o.get("symbol"),
                    "strike":  float(o.get("strike", 0)),
                    "type":    o.get("option_type"),
                    "bid":     o.get("bid", 0),
                    "ask":     o.get("ask", 0),
                    "mid":     mid,
                    "delta":   greeks.get("delta", 0),
                    "theta":   greeks.get("theta", 0),
                    "iv":      greeks.get("mid_iv", 0),
                    "oi":      o.get("open_interest", 0),
                })
            return result

    logger.error("Could not fetch option chain for expiry %s", expiry)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# ORDER MANAGEMENT
# Per official docs: multileg orders use 0-indexed legs, symbol = underlying
# Example: class=multileg&symbol=SPX&type=market&duration=day
#          &option_symbol[0]=SPXW...&side[0]=sell_to_open&quantity[0]=3
#          &option_symbol[1]=SPXW...&side[1]=buy_to_open&quantity[1]=3
#          ...
# ─────────────────────────────────────────────────────────────────────────────

def place_spread_order(
    legs: List[Dict],
    order_type: str = "market",
    limit_price: Optional[float] = None,
    duration: str = "day",
) -> Optional[Dict]:
    """
    Place a multi-leg spread order.

    legs format (each dict):
        {"symbol": "SPXW260527C05900000", "side": "sell_to_open", "quantity": 3}

    Per Tradier docs (multileg):
        - class = multileg
        - symbol = underlying ticker (SPX, not $SPX.X)
        - type = market | debit | credit | even
        - duration = day | gtc
        - option_symbol[0..3], side[0..3], quantity[0..3]  (0-indexed)
    """
    if len(legs) not in (2, 4):
        logger.error("place_spread_order: expected 2 or 4 legs, got %d", len(legs))
        return None

    data = {
        "class":    "multileg",
        "symbol":   "SPX",            # underlying — plain SPX per docs
        "type":     order_type,
        "duration": duration,
    }

    if order_type in ("debit", "credit") and limit_price is not None:
        data["price"] = str(round(limit_price, 2))

    # 0-indexed legs per official Tradier multileg spec
    for i, leg in enumerate(legs):
        data[f"option_symbol[{i}]"] = leg["symbol"]
        data[f"side[{i}]"]          = leg["side"]
        data[f"quantity[{i}]"]      = str(leg["quantity"])

    logger.info("Tradier order payload: %s", data)
    response = _post(f"/accounts/{TRADIER_ACCOUNT_ID}/orders", data)
    if not response:
        return None

    try:
        logger.info("Tradier order response: %s", response)

        if "errors" in response:
            logger.error("Tradier order error: %s", response["errors"])
            return None
        if "fault" in response:
            logger.error("Tradier fault: %s", response["fault"])
            return None

        order = response.get("order", {})
        if not order:
            logger.error("No order object in response: %s", response)
            return None

        order_id     = order.get("id")
        order_status = order.get("status", "")

        # Check for rejected status and extract reason
        if order_status == "rejected" or not order_id:
            reject_reason = (
                order.get("reject_reason") or
                order.get("reason") or
                str(response.get("errors", {}).get("error", "Unknown reason"))
            )
            logger.error(
                "Order REJECTED: id=%s status=%s reason=%s",
                order_id, order_status, reject_reason
            )
            # Attach rejection info for caller to handle
            order["_rejected"]       = True
            order["_reject_reason"]  = reject_reason
            return order

        logger.info("Order placed: id=%s status=%s", order_id, order_status)
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
    """Close an existing spread. Sides are buy_to_close / sell_to_close."""
    return place_spread_order(legs, order_type, limit_price, duration)


def get_order_status(order_id: str) -> Optional[Dict]:
    data = _get(f"/accounts/{TRADIER_ACCOUNT_ID}/orders/{order_id}")
    if not data:
        return None
    try:
        return data.get("order", {})
    except (KeyError, TypeError):
        return None


def get_account_balance() -> Optional[Dict]:
    """Returns total_equity, cash, option_buying_power."""
    data = _get(f"/accounts/{TRADIER_ACCOUNT_ID}/balances")
    if not data:
        return None
    try:
        b = data["balances"]
        return {
            "total_equity":        b.get("total_equity", 0),
            "cash":                b.get("cash", {}).get("cash_available", 0),
            "option_buying_power": b.get("option_short_value", 0),
        }
    except (KeyError, TypeError) as e:
        logger.error("Error parsing account balance: %s", e)
        return None
