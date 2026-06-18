"""
EXTRASTRIATE CORTEX — Trade Monitor & Context Broadcaster
===========================================================
Responsibilities:
  1. Monitor every active trade placed by V1
  2. Close trades early when the original pattern condition
     is violated — regardless of whether in profit or loss
  3. Detect SL/TP hits (as fallback confirmation)
  4. Update trade records with close price, time, reason, pnl
  5. Serve streamlined context data to the frontend

Condition-violation rules (per pattern):
  OB Long        → close if 5M candle closes below OB bottom
  OB Short       → close if 5M candle closes above OB top
  FVG Long       → close if 5M candle closes below FVG bottom
  FVG Short      → close if 5M candle closes above FVG top
  BB Long        → close if 5M candle closes below BB bottom
  BB Short       → close if 5M candle closes above BB top
  Double Top     → close if 5M candle closes above neckline
  Double Bottom  → close if 5M candle closes below neckline
  Trendline Long → close if 5M candle closes below trendline
  Trendline Short→ close if 5M candle closes above trendline
  POI Long       → close if 5M candle closes below POI bottom
  POI Short      → close if 5M candle closes above POI top
"""

import asyncio
from datetime import datetime
from utils.metrics import record_closed


# =========================================================
# CONFIG
# =========================================================
MONITOR_INTERVAL  = 30        # seconds between async monitor cycles
TRENDLINE_BUFFER  = 0.00005   # 0.5 pip tolerance for trendline break check


# =========================================================
# PIP VALUE — per symbol
# Using a flat PIP = 0.0001 breaks PnL on synthetics entirely.
# Volatility 100 at ~338 price: 1 "pip" equivalent ≈ 0.001
# =========================================================
def _pip_value(symbol: str) -> float:
    """Returns price-per-pip for the given symbol."""
    if "JPY" in symbol:               return 0.01
    if symbol == "XAUUSD":            return 0.10
    if symbol in ("BTCUSD", "ETHUSD"): return 1.0
    if "Volatility 10 " in symbol:    return 0.001
    if "Volatility 25 " in symbol:    return 0.01
    if "Volatility 50 " in symbol:    return 0.01
    if "Volatility 75 " in symbol:    return 0.01
    if "Volatility 100" in symbol:    return 0.01
    if "Crash" in symbol:             return 0.1
    if "Boom"  in symbol:             return 0.1
    if "Step"  in symbol:             return 0.01
    if "Jump"  in symbol:             return 0.1
    if "Range" in symbol:             return 0.01
    return 0.0001   # standard 4-decimal forex


# =========================================================
# 1. PRICE FEED
# =========================================================
def get_current_price(symbol):
    try:
        from utils.deriv_client import get_tick
        return get_tick(symbol)
    except Exception:
        return None, None, None


def get_latest_5m_candle(symbol):
    try:
        from utils.deriv_client import get_latest_candle
        return get_latest_candle(symbol, timeframe="M5")
    except Exception:
        return None


# =========================================================
# 2. CONDITION CHECKERS
# Returns (violated: bool, reason: str)
# =========================================================
def _check_ob(trade, candle):
    direction = trade["direction"]
    close     = candle["C"]
    if direction == "long"  and close < trade["zone_bottom"]:
        return True, "Price closed below OB bottom — block invalidated"
    if direction == "short" and close > trade["zone_top"]:
        return True, "Price closed above OB top — block invalidated"
    return False, None


def _check_fvg(trade, candle):
    direction = trade["direction"]
    close     = candle["C"]
    if direction == "long"  and close < trade["zone_bottom"]:
        return True, "Price closed below FVG bottom — gap no longer supporting"
    if direction == "short" and close > trade["zone_top"]:
        return True, "Price closed above FVG top — gap no longer resisting"
    return False, None


def _check_bb(trade, candle):
    direction = trade["direction"]
    close     = candle["C"]
    if direction == "long"  and close < trade["zone_bottom"]:
        return True, "Price closed below BB bottom — breaker flipped back"
    if direction == "short" and close > trade["zone_top"]:
        return True, "Price closed above BB top — breaker flipped back"
    return False, None


def _check_poi(trade, candle):
    direction = trade["direction"]
    close     = candle["C"]
    if direction == "long"  and close < trade["zone_bottom"]:
        return True, "Price closed below POI bottom — zone invalidated"
    if direction == "short" and close > trade["zone_top"]:
        return True, "Price closed above POI top — zone invalidated"
    return False, None


def _check_double_top(trade, candle):
    detail   = trade.get("lgn_signal", {}).get("source_detail", {})
    neckline = detail.get("neckline") or trade["zone_bottom"]
    if candle["C"] > neckline:
        return True, "Price closed above Double Top neckline — pattern failed"
    return False, None


def _check_double_bottom(trade, candle):
    detail   = trade.get("lgn_signal", {}).get("source_detail", {})
    neckline = detail.get("neckline") or trade["zone_top"]
    if candle["C"] < neckline:
        return True, "Price closed below Double Bottom neckline — pattern failed"
    return False, None


def _check_trendline(trade, candle):
    direction   = trade["direction"]
    detail      = trade.get("lgn_signal", {}).get("source_detail", {})
    projected_y = detail.get("projected_y") or (
        (trade["zone_top"] + trade["zone_bottom"]) / 2
    )
    close = candle["C"]
    if direction == "long"  and close < projected_y - TRENDLINE_BUFFER:
        return True, "Price closed below ascending trendline — line broken"
    if direction == "short" and close > projected_y + TRENDLINE_BUFFER:
        return True, "Price closed above descending trendline — line broken"
    return False, None


def _check_sl_tp(trade, candle):
    """Hard SL/TP check — final safety net."""
    direction = trade["direction"]
    sl        = trade.get("sl")
    tp        = trade.get("tp")
    if sl is None or tp is None:
        return False, None, None
    if direction == "long":
        if candle["L"] <= sl: return True, "sl", "Stop loss hit"
        if candle["H"] >= tp: return True, "tp", "Take profit hit"
    if direction == "short":
        if candle["H"] >= sl: return True, "sl", "Stop loss hit"
        if candle["L"] <= tp: return True, "tp", "Take profit hit"
    return False, None, None


# =========================================================
# 3. CONDITION ROUTER
# =========================================================
CONDITION_MAP = {
    "OB":            _check_ob,
    "FVG":           _check_fvg,
    "BB":            _check_bb,
    "POI":           _check_poi,
    "Double Top":    _check_double_top,
    "Double Bottom": _check_double_bottom,
    "Trendline":     _check_trendline,
}

def check_condition(trade, candle):
    checker = CONDITION_MAP.get(trade.get("pattern"))
    if checker is None:
        return False, None
    return checker(trade, candle)


# =========================================================
# 4. CLOSE TRADE ON DERIV
# =========================================================
def close_mt5_position(trade):
    try:
        from utils.deriv_client import close_position
        return close_position(trade)
    except Exception as e:
        print(f"[EC] close error: {e}")
        return False, None


# =========================================================
# 5. RECORD CLOSE
# =========================================================
def _close_record(trade, close_price, reason, exit_type="condition"):
    """
    Marks trade as closed and calculates PnL using the correct
    pip value for the symbol. Previously used a flat PIP = 0.0001
    which gave -4000 pip numbers on synthetics.
    """
    entry     = trade["entry"]
    direction = trade["direction"]
    symbol    = trade.get("symbol", "EURUSD")
    pip       = _pip_value(symbol)

    if close_price is None:
        close_price = entry

    if direction == "long":
        pnl_pips = round((close_price - entry) / pip, 1)
    else:
        pnl_pips = round((entry - close_price) / pip, 1)

    trade["status"]       = "closed"
    trade["close_price"]  = close_price
    trade["close_time"]   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    trade["close_reason"] = reason
    trade["exit_type"]    = exit_type
    trade["pnl_pips"]     = pnl_pips

    result = "WIN" if pnl_pips > 0 else "LOSS" if pnl_pips < 0 else "BE"
    print(
        f"[EC] CLOSED [{result}] {trade.get('pattern','?'):12} "
        f"{direction.upper():5} "
        f"| Entry {entry} → Close {close_price} "
        f"| PnL {pnl_pips:+.1f} pips "
        f"| {reason}"
    )

    try:
        record_closed(trade)
    except Exception as e:
        print(f"[Metrics] record_closed error: {e}")

    try:
        from utils.telegram_notifier import notify_trade_closed
        notify_trade_closed(trade)
    except Exception:
        pass

    return trade


# =========================================================
# 6. WEEKEND CLOSE
# =========================================================
_WEEKEND_CLOSE_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "XAUUSD"
}

def _should_weekend_close(symbol: str) -> bool:
    """Force-close forex/gold Friday 20:00 UTC before weekend gap risk."""
    if symbol not in _WEEKEND_CLOSE_SYMBOLS:
        return False
    now = datetime.utcnow()
    return now.weekday() == 4 and now.hour >= 20


# =========================================================
# 7. MONITOR CYCLE
# =========================================================
def monitor_cycle(active_trades, symbol):
    """
    Runs one monitoring pass over all active trades for a symbol.

    Returns:
        still_active : list of trades NOT yet closed (pass back to state)
        closed_now   : list of trades closed THIS cycle (move to closed_trades)
        context      : frontend context dict
    """
    if not active_trades:
        return [], [], build_frontend_context([], symbol)

    candle = get_latest_5m_candle(symbol)
    if candle is None:
        print(f"[EC] {symbol}: could not fetch 5M candle — skipping check")
        return active_trades, [], build_frontend_context(active_trades, symbol)

    still_active = []
    closed_now   = []

    for trade in active_trades:
        if trade.get("status") != "active":
            continue

        closed = False

        # 1. Weekend close
        if _should_weekend_close(symbol):
            close_mt5_position(trade)
            _close_record(trade, candle["C"],
                          "Weekend close — market shuts Friday 21:00 UTC",
                          exit_type="condition")
            print(f"[EC] {symbol} — weekend close executed")
            closed = True

        # 2. SL/TP hit
        if not closed:
            sl_tp_hit, exit_via, sl_tp_reason = _check_sl_tp(trade, candle)
            if sl_tp_hit:
                close_price = trade["sl"] if exit_via == "sl" else trade["tp"]
                close_mt5_position(trade)
                _close_record(trade, close_price, sl_tp_reason, exit_type=exit_via)
                closed = True

        # 3. Pattern condition violation
        if not closed:
            violated, reason = check_condition(trade, candle)
            if violated:
                close_mt5_position(trade)
                _close_record(trade, candle["C"], reason, exit_type="condition")
                closed = True

        if closed:
            closed_now.append(trade)
        else:
            still_active.append(trade)
            # Log monitoring heartbeat so we can see it's still watching
            bid, ask, mid = get_current_price(symbol)
            if mid:
                pip     = _pip_value(symbol)
                entry   = trade["entry"]
                direction = trade["direction"]
                live_pnl = round(
                    ((mid - entry) if direction == "long" else (entry - mid)) / pip, 1
                )
                print(f"[EC] {symbol} {trade.get('pattern')} "
                      f"{direction.upper()} — monitoring "
                      f"| Live PnL: {live_pnl:+.1f} pips "
                      f"| SL: {trade.get('sl')} TP: {trade.get('tp')}")

    closed_count = len(closed_now)
    if closed_count:
        print(f"[EC] {symbol}: {closed_count} trade(s) closed, "
              f"{len(still_active)} still active")

    context = build_frontend_context(still_active, symbol)
    return still_active, closed_now, context


# =========================================================
# 8. FRONTEND CONTEXT BUILDER
# =========================================================
def build_frontend_context(active_trades, symbol):
    bid, ask, mid = get_current_price(symbol)
    trade_contexts = []

    for trade in active_trades:
        entry     = trade["entry"]
        direction = trade["direction"]
        pip       = _pip_value(symbol)
        live_pnl  = None
        if mid is not None:
            live_pnl = round(
                ((mid - entry) if direction == "long" else (entry - mid)) / pip, 1
            )
        trade_contexts.append({
            "id":          trade["id"],
            "pattern":     trade["pattern"],
            "direction":   direction,
            "symbol":      symbol,
            "entry":       entry,
            "sl":          trade["sl"],
            "tp":          trade["tp"],
            "rr":          trade["rr"],
            "zone_top":    trade["zone_top"],
            "zone_bottom": trade["zone_bottom"],
            "live_pnl":    live_pnl,
            "bid":         bid,
            "ask":         ask,
            "mid":         mid,
            "confluence":  trade.get("confluence", []),
            "status":      trade["status"],
            "placed_time": trade.get("placed_time"),
            "signal_time": trade.get("signal_time"),
        })

    winning = [t for t in active_trades if (t.get("pnl_pips") or 0) > 0]
    losing  = [t for t in active_trades if (t.get("pnl_pips") or 0) < 0]

    return {
        "ts":      datetime.utcnow().isoformat(),
        "symbol":  symbol,
        "bid":     bid,
        "ask":     ask,
        "mid":     mid,
        "trades":  trade_contexts,
        "summary": {
            "total":   len(active_trades),
            "winning": len(winning),
            "losing":  len(losing),
        }
    }


# =========================================================
# 9. TRADE REGISTRY HELPERS
# =========================================================
def register_trades(trade_registry, v1_records):
    """Adds newly placed V1 trades to the shared registry."""
    added = 0
    existing_ids = {t["id"] for t in trade_registry}
    for record in v1_records:
        if record.get("placed") and record.get("status") == "active":
            if record["id"] not in existing_ids:
                trade_registry.append(record)
                existing_ids.add(record["id"])
                added += 1
                print(f"[EC] Registered trade: {record['id']}")
    return added


# =========================================================
# ASYNC MONITOR LOOP (runs inside FastAPI if used)
# =========================================================
async def monitor_loop(trade_registry, ws_manager, symbol="EURUSD"):
    print(f"[EC] Monitor loop started — checking every {MONITOR_INTERVAL}s")
    while True:
        try:
            active, closed, context = monitor_cycle(trade_registry, symbol)
            trade_registry.clear()
            trade_registry.extend(active)
            if ws_manager.active:
                await ws_manager.broadcast(context)
        except Exception as e:
            print(f"[EC] Monitor loop error: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)
