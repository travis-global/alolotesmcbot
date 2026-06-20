"""
EXTRASTRIATE CORTEX — Trade Monitor & Context Broadcaster
===========================================================
Responsibilities:
  1. Monitor every active trade placed by V1
  2. Close trades early when the original pattern condition
     is violated — regardless of whether in profit or loss
  3. Detect SL/TP hits (as fallback confirmation)
  4. Update trade records with close price, time, reason, PnL
  5. Serve streamlined context data to the frontend

Condition-violation rules (per pattern):
  OB Long         → close if 5M candle closes BELOW OB bottom
  OB Short        → close if 5M candle closes ABOVE OB top
  FVG Long        → close if 5M candle closes BELOW FVG bottom
  FVG Short       → close if 5M candle closes ABOVE FVG top
  BB Long         → close if 5M candle closes BELOW BB bottom
  BB Short        → close if 5M candle closes ABOVE BB top
  POI Long        → close if 5M candle closes BELOW POI bottom
  POI Short       → close if 5M candle closes ABOVE POI top
  Double Top      → close if 5M candle closes ABOVE zone_top (pattern failed)
  Double Bottom   → close if 5M candle closes BELOW zone_bottom (pattern failed)
  Trendline Long  → close if 5M candle closes BELOW zone_bottom (line broken)
  Trendline Short → close if 5M candle closes ABOVE zone_top (line broken)
"""

import asyncio
from datetime import datetime
from utils.metrics import record_closed


# =========================================================
# CONFIG
# =========================================================
MONITOR_INTERVAL = 30   # seconds between async monitor cycles


# =========================================================
# PIP VALUE — per symbol
# A flat 0.0001 is completely wrong for synthetics.
# Volatility 25 at ~2800: checking 5 × 0.0001 = 0.0005 pts
# as a buffer is meaningless. Each symbol needs its own value.
# =========================================================
def _pip_value(symbol: str) -> float:
    if "JPY"         in symbol: return 0.01
    if symbol == "XAUUSD":      return 0.10
    if symbol == "BTCUSD":      return 1.0
    if symbol == "ETHUSD":      return 0.10
    if "Volatility 10 " in symbol: return 0.001
    if "Volatility 25 " in symbol: return 0.01
    if "Volatility 50 " in symbol: return 0.01
    if "Volatility 75 " in symbol: return 0.01
    if "Volatility 100" in symbol: return 0.01
    if "Crash"         in symbol:  return 0.1
    if "Boom"          in symbol:  return 0.1
    if "Step"          in symbol:  return 0.01
    if "Jump"          in symbol:  return 0.1
    if "Range"         in symbol:  return 0.01
    return 0.0001   # standard 4-decimal forex


def _trendline_buffer(symbol: str) -> float:
    """
    Buffer before declaring a trendline broken.
    Must be proportional to the instrument's pip size.
    Old flat value of 0.00005 was 0.5 pips on forex and
    essentially zero on synthetics priced at hundreds.
    """
    return _pip_value(symbol) * 3   # 3 pips worth of tolerance


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
# Each returns (violated: bool, reason: str | None)
#
# Design principle: use zone_top / zone_bottom as the
# invalidation levels. These are fixed at trade placement
# and represent the structural level the setup was built on.
# Do NOT use dynamic values like projected_y for trendlines
# because the projection changes with each new candle —
# using a stale value causes phantom exits.
# =========================================================

def _check_ob(trade, candle):
    d = trade["direction"]
    c = candle["C"]
    if d == "long"  and c < trade["zone_bottom"]:
        return True, "Price closed below OB bottom — block invalidated"
    if d == "short" and c > trade["zone_top"]:
        return True, "Price closed above OB top — block invalidated"
    return False, None


def _check_fvg(trade, candle):
    d = trade["direction"]
    c = candle["C"]
    if d == "long"  and c < trade["zone_bottom"]:
        return True, "Price closed below FVG bottom — gap no longer supporting"
    if d == "short" and c > trade["zone_top"]:
        return True, "Price closed above FVG top — gap no longer resisting"
    return False, None


def _check_bb(trade, candle):
    d = trade["direction"]
    c = candle["C"]
    if d == "long"  and c < trade["zone_bottom"]:
        return True, "Price closed below BB bottom — breaker flipped back"
    if d == "short" and c > trade["zone_top"]:
        return True, "Price closed above BB top — breaker flipped back"
    return False, None


def _check_poi(trade, candle):
    d = trade["direction"]
    c = candle["C"]
    if d == "long"  and c < trade["zone_bottom"]:
        return True, "Price closed below POI bottom — zone invalidated"
    if d == "short" and c > trade["zone_top"]:
        return True, "Price closed above POI top — zone invalidated"
    return False, None


def _check_double_top(trade, candle):
    """
    Double Top is a SHORT setup. Trade was entered when price
    broke below the neckline (zone_bottom). If price recovers
    and closes ABOVE zone_top (the peaks), the pattern fully
    failed — exit immediately.
    """
    if candle["C"] > trade["zone_top"]:
        return True, "Price closed above Double Top — pattern failed"
    return False, None


def _check_double_bottom(trade, candle):
    """
    Double Bottom is a LONG setup. If price closes BELOW
    zone_bottom (the troughs), the pattern fully failed.
    """
    if candle["C"] < trade["zone_bottom"]:
        return True, "Price closed below Double Bottom — pattern failed"
    return False, None


def _check_trendline(trade, candle):
    """
    Uses zone_bottom / zone_top as the trendline reference.

    Why NOT projected_y:
    projected_y is the trendline value AT THE TIME of entry.
    Trendlines slope — one hour later the line has moved.
    A stale projected_y produces phantom exits or misses real breaks.

    zone_bottom (long) and zone_top (short) are the entry-time
    structural levels that are stable, fixed reference points.

    Buffer = 3 pips worth — allows a small wick through without
    triggering an exit on mere noise.
    """
    symbol = trade.get("symbol", "EURUSD")
    buf    = _trendline_buffer(symbol)
    d      = trade["direction"]
    c      = candle["C"]

    if d == "long"  and c < trade["zone_bottom"] - buf:
        return True, "Price closed below trendline zone — ascending line broken"
    if d == "short" and c > trade["zone_top"] + buf:
        return True, "Price closed above trendline zone — descending line broken"
    return False, None


def _check_sl_tp(trade, candle):
    """
    Hard SL/TP — final safety net.
    Uses candle High/Low for SL/TP detection (standard practice).
    SL uses H/L because a wick touching SL means the stop was hit
    intrabar even if close recovered.
    TP uses H/L by same logic — if wick reached TP, profit was there.
    """
    d  = trade.get("direction")
    sl = trade.get("sl")
    tp = trade.get("tp")

    if sl is None or tp is None or d is None:
        return False, None, None

    if d == "long":
        if candle["L"] <= sl: return True, "sl", "Stop loss hit"
        if candle["H"] >= tp: return True, "tp", "Take profit hit"
    if d == "short":
        if candle["H"] >= sl: return True, "sl", "Stop loss hit"
        if candle["L"] <= tp: return True, "tp", "Take profit hit"

    return False, None, None


# =========================================================
# 3. CONDITION ROUTER
# =========================================================
_CONDITION_MAP = {
    "OB":            _check_ob,
    "FVG":           _check_fvg,
    "BB":            _check_bb,
    "POI":           _check_poi,
    "Double Top":    _check_double_top,
    "Double Bottom": _check_double_bottom,
    "Trendline":     _check_trendline,
}


def check_condition(trade, candle):
    checker = _CONDITION_MAP.get(trade.get("pattern"))
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
        print(f"[EC] Close position error: {e}")
        return False, None


# =========================================================
# 5. RECORD CLOSE
# =========================================================
def _close_record(trade, close_price, reason, exit_type="condition"):
    """
    Marks trade as closed, calculates PnL with symbol-correct
    pip value, stamps close_time, and fires Telegram notification.

    PnL uses the close_price passed in (SL/TP level or candle
    close) — never the Deriv contract's sold_for value which
    is in USD, not price units.
    """
    entry     = trade.get("entry", 0)
    direction = trade.get("direction", "long")
    symbol    = trade.get("symbol", "EURUSD")
    pip       = _pip_value(symbol)

    if close_price is None:
        close_price = entry

    pnl_pips = round(
        ((close_price - entry) if direction == "long"
         else (entry - close_price)) / pip, 1
    )

    trade["status"]       = "closed"
    trade["close_price"]  = close_price
    trade["close_time"]   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    trade["close_reason"] = reason
    trade["exit_type"]    = exit_type
    trade["pnl_pips"]     = pnl_pips

    # Ensure placed_time exists for duration display in daily report
    if not trade.get("placed_time"):
        trade["placed_time"] = trade.get("signal_time", trade["close_time"])

    result = "WIN" if pnl_pips > 0 else "LOSS" if pnl_pips < 0 else "BE"
    print(
        f"[EC] CLOSED [{result}]  {trade.get('pattern','?'):12} "
        f"{direction.upper():5} {symbol} "
        f"| {entry} → {close_price} "
        f"| PnL {pnl_pips:+.1f} pips "
        f"| {exit_type.upper()} — {reason}"
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
    """
    Force-close forex/gold on Friday from 20:00 UTC.
    30-minute buffer before the 21:00 close to avoid
    the closing spread spike.
    Synthetics trade 24/7 — never weekend-closed.
    """
    if symbol not in _WEEKEND_CLOSE_SYMBOLS:
        return False
    now = datetime.utcnow()
    return now.weekday() == 4 and now.hour >= 20


# =========================================================
# 7. MONITOR CYCLE — main entry point
# =========================================================
def monitor_cycle(active_trades: list, symbol: str):
    """
    Single monitoring pass for all active trades of one symbol.

    Order of checks per trade:
      1. Weekend close (forex/gold only, Friday 20:00+ UTC)
      2. SL/TP hit (hard levels — immediate exit)
      3. Pattern condition violation (structural invalidity — early exit)
      4. Heartbeat log (trade still running — show live PnL)

    Returns:
      still_active  — trades still open (pass back to state)
      closed_now    — trades closed this cycle (move to closed_trades)
      context       — frontend data dict
    """
    if not active_trades:
        return [], [], _build_context([], symbol)

    candle = get_latest_5m_candle(symbol)

    if candle is None:
        print(f"[EC] {symbol}: no 5M candle — skipping monitoring this cycle")
        # Return all as still_active — do NOT close without price confirmation
        return active_trades, [], _build_context(active_trades, symbol)

    still_active = []
    closed_now   = []

    for trade in active_trades:
        if trade.get("status") != "active":
            # Trade already closed elsewhere — don't double-process
            closed_now.append(trade)
            continue

        closed = False

        # ── 1. Weekend close ───────────────────────────────
        if _should_weekend_close(symbol):
            close_mt5_position(trade)
            _close_record(
                trade, candle["C"],
                "Weekend close — forex market closes Friday 21:00 UTC",
                exit_type="condition"
            )
            closed = True

        # ── 2. SL/TP hit ───────────────────────────────────
        if not closed:
            sl_tp_hit, exit_via, sl_tp_reason = _check_sl_tp(trade, candle)
            if sl_tp_hit:
                close_price = trade["sl"] if exit_via == "sl" else trade["tp"]
                close_mt5_position(trade)
                _close_record(trade, close_price, sl_tp_reason, exit_type=exit_via)
                closed = True

        # ── 3. Pattern condition violation ─────────────────
        if not closed:
            violated, reason = check_condition(trade, candle)
            if violated:
                close_mt5_position(trade)
                _close_record(trade, candle["C"], reason, exit_type="condition")
                closed = True

        # ── 4. Still running — heartbeat ───────────────────
        if closed:
            closed_now.append(trade)
        else:
            still_active.append(trade)
            _log_heartbeat(trade, symbol)

    if closed_now:
        print(f"[EC] {symbol}: {len(closed_now)} closed, "
              f"{len(still_active)} still active")

    return still_active, closed_now, _build_context(still_active, symbol)


# =========================================================
# 8. HEARTBEAT LOG
# =========================================================
def _log_heartbeat(trade, symbol):
    bid, ask, mid = get_current_price(symbol)
    if mid is None:
        print(f"[EC] {symbol} {trade.get('pattern')} "
              f"{trade.get('direction','').upper()} — monitoring "
              f"(price unavailable)")
        return

    pip      = _pip_value(symbol)
    entry    = trade.get("entry", mid)
    direction = trade.get("direction", "long")
    live_pnl = round(
        ((mid - entry) if direction == "long" else (entry - mid)) / pip, 1
    )
    sl = trade.get("sl")
    tp = trade.get("tp")

    status = "🟢" if live_pnl > 0 else "🔴" if live_pnl < 0 else "⚪"
    print(
        f"[EC] {status} {symbol} {trade.get('pattern')} "
        f"{direction.upper()} — "
        f"PnL {live_pnl:+.1f} pips | "
        f"Price {mid} | SL {sl} | TP {tp}"
    )


# =========================================================
# 9. FRONTEND CONTEXT BUILDER
# =========================================================
def _build_context(active_trades: list, symbol: str) -> dict:
    bid, ask, mid = get_current_price(symbol)
    pip = _pip_value(symbol)

    trade_contexts = []
    for trade in active_trades:
        entry     = trade.get("entry", 0)
        direction = trade.get("direction", "long")
        live_pnl  = None
        if mid is not None:
            live_pnl = round(
                ((mid - entry) if direction == "long"
                 else (entry - mid)) / pip, 1
            )
        trade_contexts.append({
            "id":          trade.get("id"),
            "pattern":     trade.get("pattern"),
            "direction":   direction,
            "symbol":      symbol,
            "entry":       entry,
            "sl":          trade.get("sl"),
            "tp":          trade.get("tp"),
            "rr":          trade.get("rr"),
            "zone_top":    trade.get("zone_top"),
            "zone_bottom": trade.get("zone_bottom"),
            "live_pnl":    live_pnl,
            "bid":         bid,
            "ask":         ask,
            "mid":         mid,
            "confluence":  trade.get("confluence", []),
            "status":      trade.get("status"),
            "placed_time": trade.get("placed_time"),
        })

    # Live PnL-based summary (not pnl_pips which is only set on close)
    winning = sum(1 for t in trade_contexts if (t.get("live_pnl") or 0) > 0)
    losing  = sum(1 for t in trade_contexts if (t.get("live_pnl") or 0) < 0)

    return {
        "ts":      datetime.utcnow().isoformat(),
        "symbol":  symbol,
        "bid":     bid,
        "ask":     ask,
        "mid":     mid,
        "trades":  trade_contexts,
        "summary": {
            "total":   len(active_trades),
            "winning": winning,
            "losing":  losing,
        }
    }


# =========================================================
# 10. TRADE REGISTRY HELPERS
# =========================================================
def register_trades(trade_registry: list, v1_records: list) -> int:
    """
    Adds newly placed V1 trades to the active trade registry.
    Checks for duplicates by trade ID to prevent double-registration.
    Does NOT require a 'status' field — just needs placed=True and id.
    """
    existing_ids = {t.get("id") for t in trade_registry}
    added = 0
    for record in v1_records:
        if record.get("placed") and record.get("id"):
            if record["id"] not in existing_ids:
                # Ensure status is set for the monitor cycle to pick it up
                record.setdefault("status", "active")
                trade_registry.append(record)
                existing_ids.add(record["id"])
                added += 1
                print(f"[EC] Registered: {record.get('symbol')} "
                      f"{record.get('pattern')} {record.get('direction')}")
    return added


# Keep old name for backward compatibility
build_frontend_context = _build_context


# =========================================================
# ASYNC MONITOR LOOP (used by FastAPI if enabled)
# =========================================================
async def monitor_loop(trade_registry, ws_manager, symbol="EURUSD"):
    print(f"[EC] Async monitor loop started — every {MONITOR_INTERVAL}s")
    while True:
        try:
            active, closed, context = monitor_cycle(trade_registry, symbol)
            trade_registry.clear()
            trade_registry.extend(active)
            if hasattr(ws_manager, "active") and ws_manager.active:
                await ws_manager.broadcast(context)
        except Exception as e:
            print(f"[EC] Monitor loop error: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)
