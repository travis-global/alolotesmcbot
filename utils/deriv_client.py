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

DERIV_TOKEN      = os.getenv("DERIV_TOKEN",   "YOUR_DERIV_TOKEN_HERE")
DERIV_APP_ID     = os.getenv("DERIV_APP_ID",  "YOUR_APP_ID_HERE")
DERIV_ACCOUNT_ID = os.getenv("DERIV_ACCOUNT", "YOUR_ACCOUNT_ID_HERE")

# Public WebSocket — market data, no auth needed
# ws.binaryws.com is the stable Deriv market data endpoint.
# api.derivws.com is for authenticated trading only — don't use for candles/ticks.
PUBLIC_WS_URL = f"wss://ws.binaryws.com/websockets/v3?app_id=36238"
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
    "Range Break 100 Index":"rbreakdown100",
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
# OTP URL — never cached
# OTP = One Time Password. Each order needs a FRESH URL.
# Caching the URL causes HTTP 401 on the second order because
# the token in the URL is already consumed or expired.
# =========================================================
def get_otp_ws_url(account_id: str = None,
                   force_refresh: bool = True) -> str | None:
    acc = account_id or DERIV_ACCOUNT_ID
    try:
        resp = _rest_call(
            "POST",
            f"/trading/v1/options/accounts/{acc}/otp"
        )
        url = resp.get("data", {}).get("url")
        return url
    except Exception as e:
        print(f"[Deriv] get_otp_ws_url error: {e}")
        return None


# =========================================================
# MULTIPLIER LOOKUP — dynamic per symbol
# Each symbol has its own valid multiplier set (e.g. AUDUSD
# accepts 100,200,300,500,800 while V25 accepts 160,400,800...).
# We query contracts_for once per symbol, cache the lowest valid
# multiplier, and fall back to known values if the query fails.
# =========================================================
_multiplier_cache: dict = {}

# Known fallback values from observed Deriv error messages.
# Using the lowest valid multiplier per symbol = minimum leverage.
_MULTIPLIER_FALLBACK = {
    # Forex — error confirmed: 100,200,300,500,800
    "EURUSD": 100, "GBPUSD": 100, "USDJPY": 100,
    "USDCHF": 100, "AUDUSD": 100, "XAUUSD": 100,
    # Crypto — lower leverage due to high volatility
    "BTCUSD": 10,  "ETHUSD": 10,
    # Volatility indices — error confirmed V25: 160,400,800,1200,1600
    "Volatility 25 Index":  160,
    "Volatility 50 Index":  100,
    "Volatility 75 Index":   50,
    "Volatility 100 Index":  30,
    # Crash / Boom
    "Crash 500 Index":  200, "Crash 1000 Index": 100,
    "Boom 500 Index":   200, "Boom 1000 Index":  100,
    # Other synthetics
    "Step Index":            100,
    "Jump 75 Index":         100,
    "Jump 100 Index":        100,
    "Range Break 100 Index": 100,
}

async def _fetch_multiplier_async(deriv_sym: str) -> int:
    """Query contracts_for to get the lowest valid multiplier for a symbol."""
    resp = await _public_ws_call({
        "contracts_for":  deriv_sym,
        "currency":       "USD",
        "product_type":   "multiplier",
    }, timeout=15)
    available = resp.get("contracts_for", {}).get("available", [])
    multipliers = set()
    for contract in available:
        if contract.get("contract_type") in ("MULTUP", "MULTDOWN"):
            for m in contract.get("multiplier_range", []):
                multipliers.add(int(m))
    return min(multipliers) if multipliers else 0


def get_valid_multiplier(symbol: str) -> int:
    """
    Returns the lowest valid multiplier for a symbol.
    Queries Deriv's contracts_for on first call, then caches the result.
    Falls back to _MULTIPLIER_FALLBACK if the query fails.
    """
    if symbol in _multiplier_cache:
        return _multiplier_cache[symbol]

    deriv_sym = to_deriv_symbol(symbol)
    try:
        mult = _run(_fetch_multiplier_async(deriv_sym))
        if mult > 0:
            _multiplier_cache[symbol] = mult
            print(f"[Deriv] {symbol} valid multipliers → using lowest: {mult}")
            return mult
    except Exception as e:
        print(f"[Deriv] multiplier lookup failed for {symbol}: {e}")

    # Fall back to known map
    mult = _MULTIPLIER_FALLBACK.get(symbol, 100)
    _multiplier_cache[symbol] = mult
    print(f"[Deriv] {symbol} using fallback multiplier: {mult}")
    return mult


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
                              stake: float,
                              entry: float = None,
                              sl: float    = None,
                              tp: float    = None) -> tuple:
    """
    Places a Deriv multiplier contract via the OTP WebSocket.

    Contract type: MULTUP (long) / MULTDOWN (short)
    Always gets a fresh OTP URL — OTP tokens are single-use.

    SL/TP are passed as limit_order amounts in USD:
        sl_usd = stake × multiplier × |entry - sl| / entry
        tp_usd = stake × multiplier × |tp    - entry| / entry
    """
    ws_url = get_otp_ws_url()
    if not ws_url:
        print(f"[Deriv] {symbol}: OTP URL is None — check DERIV_ACCOUNT and token")
        return False, {"error": "Could not get OTP WebSocket URL"}

    import websockets

    deriv_sym     = to_deriv_symbol(symbol)
    contract_type = "MULTUP" if direction == "long" else "MULTDOWN"
    multiplier    = get_valid_multiplier(symbol)   # lowest valid for this symbol

    # ── Auto-scale stake to guarantee SL/TP always gets set on Deriv ────
    # Deriv requires limit_order amounts >= $0.10.
    # With a flat $1 stake, an 8-pip SL on EURUSD gives sl_usd = $0.069
    # which is below the minimum → limit_order gets skipped → NO PROTECTION.
    # Instead, calculate the minimum stake that makes both sl_usd and tp_usd
    # >= $0.10, then use whichever is larger (sl or tp needs the bigger stake).
    MIN_LIMIT_USD = 0.10
    base_stake    = max(stake, 1.0)   # absolute floor is $1
    MAX_STAKE     = 10.0              # above this, risk per trade is too high
                                      # for a demo/small account

    actual_stake = base_stake
    if entry and sl and tp:
        try:
            sl_ratio = abs(entry - sl) / entry
            tp_ratio = abs(tp - entry) / entry
            if sl_ratio > 0 and tp_ratio > 0:
                min_for_sl = MIN_LIMIT_USD / (multiplier * sl_ratio)
                min_for_tp = MIN_LIMIT_USD / (multiplier * tp_ratio)
                actual_stake = max(base_stake, min_for_sl, min_for_tp)
                actual_stake = round(actual_stake, 2)
                if actual_stake > base_stake:
                    print(f"[Deriv] {symbol}: stake scaled "
                          f"${base_stake} → ${actual_stake} to meet SL/TP minimum")
        except Exception:
            pass

    # Guard: if stake is still too high the setup's risk profile
    # doesn't suit our capital. Skip rather than place a bad trade.
    # The 1-2 second closes happen when spread eats a tiny stake
    # on high-multiplier instruments — this prevents that.
    if actual_stake > MAX_STAKE:
        print(f"[Deriv] {symbol}: stake ${actual_stake} exceeds "
              f"max ${MAX_STAKE} — skipping trade")
        return False, {"error": f"Stake ${actual_stake} too high (max ${MAX_STAKE})"}

    # Required fields per official schema — underlying_symbol NOT symbol
    params = {
        "contract_type":     contract_type,
        "currency":          "USD",
        "underlying_symbol": deriv_sym,
        "amount":            actual_stake,
        "basis":             "stake",
        "multiplier":        multiplier,
    }

    # Attach SL/TP as limit_order — stake was scaled above so both values
    # are guaranteed to be >= $0.10 (Deriv minimum).
    if entry and sl and tp:
        try:
            position_value = actual_stake * multiplier
            sl_usd = round(position_value * abs(entry - sl) / entry, 2)
            tp_usd = round(position_value * abs(tp - entry) / entry, 2)
            params["limit_order"] = {
                "stop_loss":   sl_usd,
                "take_profit": tp_usd,
            }
            print(f"[Deriv] {symbol}: SL=${sl_usd} TP=${tp_usd} set on Deriv")
        except Exception:
            pass

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"buy": "1", "price": actual_stake, "parameters": params}))

        deadline = asyncio.get_event_loop().time() + 15
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False, {"error": "Order timeout"}

            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)

            if resp.get("error"):
                error_msg = resp["error"].get("message", str(resp["error"]))
                print(f"[Deriv] Order rejected — {symbol} {direction}: {error_msg}")

                # Auto-correct multiplier from error message and signal retry.
                # Deriv says e.g. "Multiplier is not in acceptable range. Accepts 750,2000,3500"
                # Parse those values, update cache, signal place_order to retry.
                if "Multiplier is not in acceptable range" in error_msg:
                    import re
                    match = re.search(r"Accepts ([\d,]+)", error_msg)
                    if match:
                        valid = [int(x.strip()) for x in match.group(1).split(",")]
                        correct = min(valid)
                        _multiplier_cache[symbol] = correct
                        print(f"[Deriv] {symbol}: multiplier auto-corrected "
                              f"{multiplier} → {correct}, will retry")
                        return False, {"error": f"__mult_retry__{correct}"}

                return False, {"error": error_msg}

            if "buy" in resp:
                b = resp["buy"]
                return True, {
                    "contract_id":   b.get("contract_id"),
                    "buy_price":     b.get("buy_price"),
                    "balance_after": b.get("balance_after"),
                    "longcode":      b.get("longcode"),
                }


def place_order(symbol: str, direction: str,
                stake: float  = 1.0,
                entry: float  = None,
                sl: float     = None,
                tp: float     = None) -> tuple:
    try:
        success, result = _run(
            _place_order_async(symbol, direction, stake, entry, sl, tp)
        )
        # Auto-retry once if multiplier was corrected from Deriv's error message
        if (not success
                and isinstance(result.get("error"), str)
                and result["error"].startswith("__mult_retry__")):
            print(f"[Deriv] {symbol}: retrying with corrected multiplier")
            success, result = _run(
                _place_order_async(symbol, direction, stake, entry, sl, tp)
            )
        return success, result
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


# =========================================================
# FETCH DAILY TRADE HISTORY FROM DERIV
# Uses the OTP WebSocket profit_table call — returns actual
# closed contracts from Deriv's own records, not bot state.
# =========================================================
# Reverse map — Deriv internal symbol → display name
_REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}

def _display_symbol(underlying: str) -> str:
    """Converts Deriv internal symbol (e.g. R_25) to display name."""
    return _REVERSE_SYMBOL_MAP.get(underlying, underlying)


async def _fetch_daily_trades_async(date_str: str) -> list:
    """
    Fetches all closed contracts for a given date from Deriv's
    profit_table endpoint via OTP WebSocket.

    Returns list of enriched trade dicts with:
      - display_symbol : human-readable pair name (e.g. Volatility 25 Index)
      - underlying     : Deriv internal symbol (e.g. R_25)
      - direction      : LONG or SHORT
      - contract_type  : MULTUP / MULTDOWN
      - contract_id    : used to cross-reference with bot's closed_trades
      - buy_price      : stake in USD
      - sell_price     : payout in USD
      - profit         : net profit/loss in USD
      - buy_time       : HH:MM UTC open time
      - sell_time      : HH:MM UTC close time
      - buy_ts         : raw Unix timestamp (for matching with bot state)
      - duration       : human-readable duration
      - longcode       : Deriv's full contract description
      - win            : True if profit > 0
    """
    day_start = datetime.strptime(date_str, "%Y-%m-%d")
    day_end   = datetime(day_start.year, day_start.month, day_start.day, 23, 59, 59)
    ts_from   = int(day_start.timestamp())
    ts_to     = int(day_end.timestamp())

    ws_url = get_otp_ws_url()
    if not ws_url:
        print("[Deriv] fetch_daily_trades: no OTP URL")
        return []

    import websockets

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "profit_table": 1,
            "description":  1,
            "sort":         "ASC",
            "date_from":    ts_from,
            "date_to":      ts_to,
            "limit":        100,
        }))

        deadline = asyncio.get_event_loop().time() + 20
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return []
            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)

            if resp.get("error"):
                print(f"[Deriv] profit_table error: {resp['error'].get('message')}")
                return []

            if resp.get("msg_type") == "profit_table":
                trades = []
                for t in resp.get("profit_table", {}).get("transactions", []):
                    buy_price  = float(t.get("buy_price",  0) or 0)
                    sell_price = float(t.get("sell_price", 0) or 0)
                    profit     = round(sell_price - buy_price, 2)

                    buy_ts  = t.get("purchase_time") or t.get("buy_time",  0)
                    sell_ts = t.get("sell_time", 0)

                    buy_time  = datetime.utcfromtimestamp(buy_ts ).strftime("%H:%M") if buy_ts  else "—"
                    sell_time = datetime.utcfromtimestamp(sell_ts).strftime("%H:%M") if sell_ts else "—"

                    duration = ""
                    if buy_ts and sell_ts:
                        mins = int((sell_ts - buy_ts) / 60)
                        duration = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
                        if mins == 0:
                            secs = int(sell_ts - buy_ts)
                            duration = f"{secs}s"

                    contract_type = t.get("contract_type", "")
                    underlying    = t.get("underlying", "")

                    trades.append({
                        "display_symbol": _display_symbol(underlying),
                        "underlying":     underlying,
                        "direction":      "LONG" if "UP" in contract_type else "SHORT",
                        "contract_type":  contract_type,
                        "contract_id":    t.get("transaction_id") or t.get("contract_id"),
                        "buy_price":      buy_price,
                        "sell_price":     sell_price,
                        "profit":         profit,
                        "buy_time":       buy_time,
                        "sell_time":      sell_time,
                        "buy_ts":         buy_ts,
                        "sell_ts":        sell_ts,
                        "duration":       duration,
                        "longcode":       t.get("longcode", ""),
                        "win":            profit > 0,
                    })
                return trades


def fetch_daily_trades(date_str: str) -> list:
    """Fetches closed contracts from Deriv for a given date. Returns [] on failure."""
    try:
        return _run(_fetch_daily_trades_async(date_str))
    except Exception as e:
        print(f"[Deriv] fetch_daily_trades error: {e}")
        return []


def merge_with_daily_log(deriv_trades: list, log_trades: list) -> list:
    """
    Cross-references Deriv's profit_table records with the bot's
    daily_log (written at trade placement time) to produce one
    complete record per trade.

    Deriv provides: financial outcome (stake, payout, profit, times)
    Daily log provides: why the trade was taken (pattern, entry,
                        SL, TP, R:R, confluence)

    Matching strategy:
      1. contract_id match — most reliable (stored in daily_log
         from Deriv's buy response, returned in profit_table)
      2. symbol + direction + open time within ±5 minutes — fallback
         for cases where contract_id format differs between endpoints

    Unmatched Deriv trades still appear in the report — they just
    show "—" for bot-side fields.
    """
    if not log_trades:
        return deriv_trades

    # Build lookup by contract_id from daily log
    log_by_cid = {}
    for lt in log_trades:
        cid = lt.get("contract_id")
        if cid:
            log_by_cid[str(cid)] = lt

    # Build fallback lookup by symbol + direction
    log_by_sym = {}
    for lt in log_trades:
        sym = lt.get("symbol", "")
        log_by_sym.setdefault(sym, []).append(lt)

    def _log_ts(lt):
        placed = lt.get("placed_at", "")
        try:
            return datetime.strptime(placed[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return 0

    merged = []
    for dt in deriv_trades:
        match = None

        # Try contract_id
        cid = str(dt.get("contract_id") or "")
        if cid and cid in log_by_cid:
            match = log_by_cid[cid]

        # Fallback: symbol + direction + time
        if not match:
            display   = dt.get("display_symbol", "")
            direction = dt.get("direction", "").lower()
            buy_ts    = dt.get("buy_ts", 0)
            for lt in log_by_sym.get(display, []):
                if lt.get("direction", "").lower() != direction:
                    continue
                lt_ts = _log_ts(lt)
                if lt_ts and abs(buy_ts - lt_ts) <= 300:   # 5-minute window
                    match = lt
                    break

        if match:
            dt["pattern"]    = match.get("pattern",    "—")
            dt["confluence"] = " | ".join(match.get("confluence") or []) or "—"
            dt["entry"]      = match.get("entry")
            dt["sl"]         = match.get("sl")
            dt["tp"]         = match.get("tp")
            dt["rr"]         = match.get("rr")
            dt["multiplier"] = match.get("multiplier")
            dt["placed_at"]  = match.get("placed_at",  "—")
            dt["zone_top"]   = match.get("zone_top")
            dt["zone_bottom"]= match.get("zone_bottom")
        else:
            dt.setdefault("pattern",    "—")
            dt.setdefault("confluence", "—")
            dt.setdefault("entry",      None)
            dt.setdefault("sl",         None)
            dt.setdefault("tp",         None)
            dt.setdefault("rr",         None)
            dt.setdefault("multiplier", None)
            dt.setdefault("placed_at",  "—")

        merged.append(dt)

    return merged


# =========================================================
# UPDATE CONTRACT STOP LOSS (trailing stop)
# =========================================================
async def _update_contract_sl_async(contract_id: int,
                                     sl_usd: float,
                                     tp_usd: float) -> bool:
    """Updates limit_order on an existing multiplier contract."""
    ws_url = get_otp_ws_url()
    if not ws_url:
        return False

    import websockets
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "contract_update": 1,
            "contract_id": contract_id,
            "limit_order": {
                "stop_loss":   sl_usd,
                "take_profit": tp_usd,
            }
        }))
        deadline = asyncio.get_event_loop().time() + 10
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            raw  = await asyncio.wait_for(ws.recv(), timeout=remaining)
            resp = json.loads(raw)
            if resp.get("error"):
                print(f"[Deriv] contract_update error: "
                      f"{resp['error'].get('message')}")
                return False
            if resp.get("msg_type") == "contract_update":
                return True


def update_contract_sl(contract_id: int,
                       sl_usd: float,
                       tp_usd: float) -> bool:
    """Updates stop_loss and take_profit on a live Deriv contract."""
    try:
        return _run(_update_contract_sl_async(contract_id, sl_usd, tp_usd))
    except Exception as e:
        print(f"[Deriv] update_contract_sl error: {e}")
        return False
