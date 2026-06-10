"""
deriv_api.py — Deriv API data layer
======================================
Three endpoints used:

1. PUBLIC WebSocket — market data (no auth)
   wss://api.derivws.com/trading/v1/options/ws/public
   Used for: ticks, candle history

2. OTP WebSocket — trading (auth via OTP)
   wss://api.derivws.com/trading/v1/options/ws/demo?otp=...
   Used for: place order, close order

3. REST API — account management (auth via Bearer token)
   https://api.derivws.com
   Used for: get accounts, get OTP URL
"""

import asyncio
import json
import os
import threading
import urllib.request
import urllib.error
from datetime import datetime

DERIV_TOKEN      = os.getenv("DERIV_TOKEN",   "")
DERIV_APP_ID     = os.getenv("DERIV_APP_ID",  "")
DERIV_ACCOUNT_ID = os.getenv("DERIV_ACCOUNT", "")

# Public WebSocket — no auth needed
PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
REST_BASE_URL = "https://api.derivws.com"

SYMBOL_MAP = {
    "EURUSD":               "frxEURUSD",
    "GBPUSD":               "frxGBPUSD",
    "USDJPY":               "frxUSDJPY",
    "USDCHF":               "frxUSDCHF",
    "AUDUSD":               "frxAUDUSD",
    "XAUUSD":               "frxXAUUSD",
    "BTCUSD":               "cryBTCUSD",
    "ETHUSD":               "cryETHUSD",
    "Volatility 25 Index":  "R_25",
    "Volatility 50 Index":  "R_50",
    "Volatility 75 Index":  "R_75",
    "Volatility 100 Index": "R_100",
    "Crash 500 Index":      "CRASH500",
    "Crash 1000 Index":     "CRASH1000",
    "Boom 500 Index":       "BOOM500",
    "Boom 1000 Index":      "BOOM1000",
    "Step Index":           "stpRNG",
    "Jump 75 Index":        "JD75",
    "Jump 100 Index":       "JD100",
    "Range Break 100 Index":"rangebreak100",
}

TIMEFRAME_MAP = {
    "M1":  60,
    "M5":  300,
    "M15": 900,
    "H1":  3600,
    "H4":  14400,
    "D1":  86400,
}

SYNTHETIC_SPREADS = {
    "Volatility 75 Index":  0.010,
    "Volatility 100 Index": 0.020,
    "Volatility 25 Index":  0.005,
    "Volatility 50 Index":  0.008,
    "Crash 1000 Index":     0.500,
    "Crash 500 Index":      0.500,
    "Boom 1000 Index":      0.500,
    "Boom 500 Index":       0.500,
    "Step Index":           0.100,
    "Jump 75 Index":        0.050,
    "Jump 100 Index":       0.050,
    "Range Break 100 Index":0.100,
}

def to_deriv_symbol(symbol):
    return SYMBOL_MAP.get(symbol, symbol)


# =========================================================
# THREAD-SAFE ASYNC RUNNER
# =========================================================
def _run(coro):
    result_holder = {}

    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_holder["result"] = loop.run_until_complete(coro)
        except Exception as e:
            result_holder["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=run_in_thread)
    t.start()
    t.join(timeout=30)

    if "error" in result_holder:
        raise result_holder["error"]
    if "result" not in result_holder:
        raise TimeoutError("Deriv API call timed out")
    return result_holder["result"]


# Request key → expected response msg_type
# Deriv uses different names for request vs response
_MSG_TYPE_MAP = {
    "ticks":          "tick",      # {"ticks":"R_75"} → msg_type:"tick"
    "ticks_history":  "candles",   # {"ticks_history":...} → msg_type:"candles"
    "ping":           "ping",
    "time":           "time",
    "active_symbols": "active_symbols",
}

# =========================================================
# PUBLIC WEBSOCKET CALL — no auth, market data only
# =========================================================
async def _public_ws_call(payload: dict, timeout: int = 45) -> dict:
    """
    Connects to Deriv public WebSocket endpoint.
    No authentication required.
    Used for: ticks, candle history, active symbols.
    """
    import websockets

    req_key       = list(payload.keys())[0]
    expected_type = _MSG_TYPE_MAP.get(req_key, req_key)

    async with websockets.connect(PUBLIC_WS_URL) as ws:
        await ws.send(json.dumps(payload))

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Public WS timeout waiting for {expected_type}")

            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)

            if resp.get("error"):
                raise RuntimeError(resp["error"]["message"])

            if resp.get("msg_type") == expected_type:
                return resp


# =========================================================
# REST API — authenticated
# =========================================================
def _rest_call(method: str, path: str, body: dict = None) -> dict:
    url     = f"{REST_BASE_URL}{path}"
    headers = {
        "Content-Type":  "application/json",
        "Deriv-App-ID":  DERIV_APP_ID,
        "Authorization": f"Bearer {DERIV_TOKEN}",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"REST {e.code}: {e.read().decode()}")


# =========================================================
# OTP URL — cached
# =========================================================
_otp_cache = {"url": None}

def get_otp_ws_url(account_id: str = None,
                   force_refresh: bool = False) -> str | None:
    acc = account_id or DERIV_ACCOUNT_ID

    if _otp_cache["url"] and not force_refresh:
        return _otp_cache["url"]

    try:
        resp = _rest_call(
            "POST",
            f"/trading/v1/options/accounts/{acc}/otp"
        )
        url = resp.get("data", {}).get("url")
        if url:
            _otp_cache["url"] = url
        return url
    except Exception as e:
        print(f"[Deriv] get_otp_ws_url error: {e}")
        return None


# =========================================================
# 1. FETCH OHLC CANDLES — public WS
# =========================================================
async def _fetch_candles_async(symbol: str, timeframe: str,
                                count: int) -> list:
    deriv_sym   = to_deriv_symbol(symbol)
    granularity = TIMEFRAME_MAP.get(timeframe.upper(), 14400)

    resp = await _public_ws_call({
        "ticks_history":     deriv_sym,
        "style":             "candles",
        "granularity":       granularity,
        "count":             count,
        "end":               "latest",
        "adjust_start_time": 1
    })

    return [{
        "time": datetime.fromtimestamp(c["epoch"]).strftime("%Y-%m-%d %H:%M:%S"),
        "O":    round(float(c["open"]),  5),
        "H":    round(float(c["high"]),  5),
        "L":    round(float(c["low"]),   5),
        "C":    round(float(c["close"]), 5)
    } for c in resp.get("candles", [])]


def fetch_candles(symbol: str, timeframe: str = "H4",
                  count: int = 500) -> list:
    try:
        return _run(_fetch_candles_async(symbol, timeframe, count))
    except Exception as e:
        print(f"[Deriv] fetch_candles error ({symbol}): {type(e).__name__}: {e}")
        return []


# =========================================================
# 2. GET CURRENT TICK — public WS
# =========================================================
async def _get_tick_async(symbol: str) -> tuple:
    deriv_sym = to_deriv_symbol(symbol)
    resp      = await _public_ws_call({"ticks": deriv_sym})
    tick      = resp.get("tick", {})

    if not tick:
        return None, None, None

    bid = float(tick.get("bid") or tick.get("quote") or 0)
    ask = float(tick.get("ask") or tick.get("quote") or 0)

    if bid == ask and bid > 0:
        spread = SYNTHETIC_SPREADS.get(symbol, 0.00010)
        bid    = round(bid - spread / 2, 5)
        ask    = round(ask + spread / 2, 5)

    mid = round((bid + ask) / 2, 5)
    return round(bid, 5), round(ask, 5), mid


def get_tick(symbol: str) -> tuple:
    try:
        return _run(_get_tick_async(symbol))
    except Exception as e:
        print(f"[Deriv] get_tick error ({symbol}): {type(e).__name__}: {e}")
        return None, None, None


# =========================================================
# 3. GET LATEST CLOSED CANDLE
# =========================================================
def get_latest_candle(symbol: str, timeframe: str = "M5") -> dict | None:
    candles = fetch_candles(symbol, timeframe=timeframe, count=2)
    return candles[-2] if len(candles) >= 2 else None


# =========================================================
# 4. GET ACCOUNTS
# =========================================================
def get_accounts() -> list:
    try:
        return _rest_call("GET", "/trading/v1/options/accounts").get("data", [])
    except Exception as e:
        print(f"[Deriv] get_accounts error: {e}")
        return []


# =========================================================
# 5. PLACE ORDER — OTP WebSocket
# =========================================================
async def _place_order_async(symbol: str, direction: str,
                              stake: float) -> tuple:
    ws_url = get_otp_ws_url()
    if not ws_url:
        return False, {"error": "Could not get OTP WebSocket URL"}

    import websockets

    deriv_sym     = to_deriv_symbol(symbol)
    contract_type = "MULTUP" if direction == "long" else "MULTDOWN"

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "buy": 1,
            "price": stake,
            "parameters": {
                "contract_type": contract_type,
                "symbol":        deriv_sym,
                "amount":        stake,
                "currency":      "USD",
                "duration":      0,
                "duration_unit": "t",
                "multiplier":    10,
            }
        }))

        deadline = asyncio.get_event_loop().time() + 15
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False, {"error": "Order timeout"}

            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)

            if resp.get("error"):
                return False, {"error": resp["error"]["message"]}

            if "buy" in resp:
                b = resp["buy"]
                return True, {
                    "contract_id":   b.get("contract_id"),
                    "buy_price":     b.get("buy_price"),
                    "balance_after": b.get("balance_after"),
                    "longcode":      b.get("longcode"),
                }


def place_order(symbol: str, direction: str,
                stake: float = 1.0) -> tuple:
    try:
        return _run(_place_order_async(symbol, direction, stake))
    except Exception as e:
        print(f"[Deriv] place_order error ({symbol}): {e}")
        return False, {"error": str(e)}


# =========================================================
# 6. CLOSE POSITION — OTP WebSocket
# =========================================================
async def _close_position_async(contract_id: int) -> tuple:
    ws_url = get_otp_ws_url()
    if not ws_url:
        return False, None

    import websockets

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"sell": contract_id, "price": 0}))

        deadline = asyncio.get_event_loop().time() + 15
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False, None

            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)

            if resp.get("error"):
                return False, None

            if "sell" in resp:
                return True, round(float(resp["sell"].get("sold_for", 0)), 5)


def close_position(trade: dict) -> tuple:
    try:
        result      = trade.get("mt5_result", {})
        contract_id = result.get("contract_id") if isinstance(result, dict) else None
        if not contract_id:
            return False, None
        return _run(_close_position_async(contract_id))
    except Exception as e:
        print(f"[Deriv] close_position error: {e}")
        return False, None


# =========================================================
# QUICK TEST
# =========================================================
if __name__ == "__main__":

    # Raw connection test first
    print("=" * 55)
    print("0. Raw public WS connection test")
    print("=" * 55)
    async def _raw_test():
        import websockets
        print(f"  Connecting to: {PUBLIC_WS_URL}")
        try:
            async with websockets.connect(PUBLIC_WS_URL) as ws:
                print("  Connected OK")
                await ws.send(json.dumps({"ping": 1}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                print(f"  Ping response: {resp}")
        except Exception as e:
            print(f"  Connection failed: {type(e).__name__}: {e}")
    _run(_raw_test())
    print("=" * 55)
    print("1. Live ticks (public WS — no auth)")
    print("=" * 55)
    for sym in ["EURUSD", "Volatility 75 Index", "BTCUSD"]:
        bid, ask, mid = get_tick(sym)
        if mid:
            print(f"  {sym:30} Bid:{bid}  Ask:{ask}  Mid:{mid}")
        else:
            print(f"  {sym:30} — failed")

    print()
    print("=" * 55)
    print("2. Candle history (public WS — no auth)")
    print("=" * 55)
    candles = fetch_candles("Volatility 75 Index", "H4", count=3)
    if candles:
        for c in candles:
            print(f"  {c['time']}  O:{c['O']} H:{c['H']} L:{c['L']} C:{c['C']}")
    else:
        print("  No candles returned")

    print()
    print("=" * 55)
    print("3. Accounts (REST — authenticated)")
    print("=" * 55)
    accounts = get_accounts()
    if accounts:
        for a in accounts:
            print(f"  {a}")
    else:
        print("  No accounts — check DERIV_TOKEN and DERIV_APP_ID in .env")

    print()
    print("=" * 55)
    print("4. OTP WebSocket URL (REST — authenticated)")
    print("=" * 55)
    url = get_otp_ws_url()
    if url:
        print(f"  OTP URL: {url[:55]}...")
    else:
        print("  OTP failed — check DERIV_ACCOUNT in .env")
