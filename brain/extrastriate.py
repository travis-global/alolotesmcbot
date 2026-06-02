"""
EXTRASTRIATE CORTEX — Trade Monitor & Context Broadcaster
===========================================================
Responsibilities:
  1. Monitor every active trade placed by V1
  2. Close trades early when the original pattern condition
     is violated — regardless of whether in profit or loss
  3. Detect SL/TP hits (as fallback confirmation)
  4. Update trade records with close price, time, reason, pnl
  5. Serve streamlined context data to the frontend via WebSocket
     — only the data relevant to active positions, nothing more

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

All conditions checked on every 5M candle close.
SL and TP are also monitored as the final safety net.
"""

import asyncio
from datetime import datetime
from utils.metrics import record_closed


# =========================================================
# CONFIG
# =========================================================
PIP               = 0.00010   # 1 pip for 5-decimal pairs
MONITOR_INTERVAL  = 30        # seconds between monitor cycles (≈ half a 5M candle)
TRENDLINE_BUFFER  = 0.00005   # 0.5 pip tolerance for trendline break check


# =========================================================
# 1. FETCH CURRENT PRICE
# =========================================================
def get_current_price(symbol):
    """
    Gets the latest bid/ask from Deriv API.
    Returns (bid, ask, mid) or (None, None, None) on failure.
    """
    try:
        from utils.deriv_client import get_tick
        return get_tick(symbol)
    except Exception:
        return None, None, None


def get_latest_5m_candle(symbol):
    """
    Fetches the most recently closed 5M candle from Deriv API.
    Returns candle dict or None.
    """
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
    direction  = trade["direction"]
    ob_top     = trade["zone_top"]
    ob_bottom  = trade["zone_bottom"]
    close      = candle["C"]

    if direction == "long" and close < ob_bottom:
        return True, "Price closed below OB bottom — block invalidated"
    if direction == "short" and close > ob_top:
        return True, "Price closed above OB top — block invalidated"
    return False, None


def _check_fvg(trade, candle):
    direction  = trade["direction"]
    fvg_top    = trade["zone_top"]
    fvg_bottom = trade["zone_bottom"]
    close      = candle["C"]

    if direction == "long" and close < fvg_bottom:
        return True, "Price closed below FVG bottom — gap no longer supporting"
    if direction == "short" and close > fvg_top:
        return True, "Price closed above FVG top — gap no longer resisting"
    return False, None


def _check_bb(trade, candle):
    direction  = trade["direction"]
    bb_top     = trade["zone_top"]
    bb_bottom  = trade["zone_bottom"]
    close      = candle["C"]

    if direction == "long" and close < bb_bottom:
        return True, "Price closed below BB bottom — breaker flipped back"
    if direction == "short" and close > bb_top:
        return True, "Price closed above BB top — breaker flipped back"
    return False, None


def _check_poi(trade, candle):
    direction  = trade["direction"]
    poi_top    = trade["zone_top"]
    poi_bottom = trade["zone_bottom"]
    close      = candle["C"]

    if direction == "long" and close < poi_bottom:
        return True, "Price closed below POI bottom — zone invalidated"
    if direction == "short" and close > poi_top:
        return True, "Price closed above POI top — zone invalidated"
    return False, None


def _check_double_top(trade, candle):
    # Source detail has the neckline
    detail   = trade.get("lgn_signal", {}).get("source_detail", {})
    neckline = detail.get("neckline") or trade["zone_bottom"]
    close    = candle["C"]

    if close > neckline:
        return True, "Price closed above Double Top neckline — pattern failed"
    return False, None


def _check_double_bottom(trade, candle):
    detail   = trade.get("lgn_signal", {}).get("source_detail", {})
    neckline = detail.get("neckline") or trade["zone_top"]
    close    = candle["C"]

    if close < neckline:
        return True, "Price closed below Double Bottom neckline — pattern failed"
    return False, None


def _check_trendline(trade, candle):
    direction = trade["direction"]
    detail    = trade.get("lgn_signal", {}).get("source_detail", {})

    # Get trendline projected value at current candle
    # We use the stored projected_y as reference — it was valid at signal time
    projected_y = detail.get("projected_y")

    if projected_y is None:
        # Fall back to zone midpoint
        projected_y = (trade["zone_top"] + trade["zone_bottom"]) / 2

    close = candle["C"]

    if direction == "long" and close < projected_y - TRENDLINE_BUFFER:
        return True, "Price closed below ascending trendline — line broken"
    if direction == "short" and close > projected_y + TRENDLINE_BUFFER:
        return True, "Price closed above descending trendline — line broken"
    return False, None


def _check_sl_tp(trade, candle):
    """
    Safety net — detects SL or TP hit on the latest candle.
    MT5 handles this natively but we track it here for record accuracy.
    """
    direction = trade["direction"]
    sl        = trade["sl"]
    tp        = trade["tp"]

    if sl is None or tp is None:
        return False, None, None

    if direction == "long":
        if candle["L"] <= sl:
            return True, "sl", "Stop loss hit"
        if candle["H"] >= tp:
            return True, "tp", "Take profit hit"

    if direction == "short":
        if candle["H"] >= sl:
            return True, "sl", "Stop loss hit"
        if candle["L"] <= tp:
            return True, "tp", "Take profit hit"

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
    """
    Routes to the correct condition checker for the trade pattern.
    Returns (violated: bool, reason: str or None)
    """
    pattern = trade.get("pattern")
    checker = CONDITION_MAP.get(pattern)

    if checker is None:
        return False, None

    return checker(trade, candle)


# =========================================================
# 4. CLOSE TRADE ON DERIV
# =========================================================
def close_mt5_position(trade):
    """
    Closes an open Deriv contract.
    Named close_mt5_position for drop-in compatibility.
    Returns (success: bool, close_price: float or None)
    """
    try:
        from utils.deriv_client import close_position
        return close_position(trade)
    except Exception as e:
        print(f"[EC] close error: {e}")
        return False, None


# =========================================================
# 5. RECORD UPDATER
# =========================================================
def _close_record(trade, close_price, reason, exit_type="condition"):
    """
    Updates the trade record in place with close details.
    Calculates pnl in pips.
    """
    entry     = trade["entry"]
    direction = trade["direction"]

    if close_price is None:
        # Estimate from current known price if MT5 didn't return one
        close_price = entry

    if direction == "long":
        pnl_pips = round((close_price - entry) / PIP, 1)
    else:
        pnl_pips = round((entry - close_price) / PIP, 1)

    trade["status"]       = "closed"
    trade["close_price"]  = close_price
    trade["close_time"]   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    trade["close_reason"] = reason
    trade["exit_type"]    = exit_type   # "condition" | "sl" | "tp" | "manual"
    trade["pnl_pips"]     = pnl_pips

    result = "WIN" if pnl_pips > 0 else "LOSS" if pnl_pips < 0 else "BE"
    print(
        f"[EC] CLOSED [{result}] {trade['pattern']:12} "
        f"{direction.upper():5} "
        f"| Entry {entry:.5f} → Close {close_price:.5f} "
        f"| PnL {pnl_pips:+.1f} pips "
        f"| {reason}"
    )

    # Record to metrics file
    try:
        record_closed(trade)
    except Exception as e:
        print(f"[Metrics] record_closed error: {e}")

    return trade


# =========================================================
# 6. CONTEXT BUILDER FOR FRONTEND
# =========================================================
def build_frontend_context(active_trades, symbol):
    """
    Builds the streamlined payload sent to the frontend WebSocket.
    Only includes data relevant to active positions — no raw Retina dump.

    Frontend receives:
      - Live bid/ask/mid prices
      - Per-trade context: pattern, direction, entry, SL, TP, zone,
        current pnl, condition status, confluence
      - Summary stats
    """
    bid, ask, mid = get_current_price(symbol)

    trade_contexts = []

    for trade in active_trades:
        entry     = trade["entry"]
        direction = trade["direction"]

        # Live PnL in pips
        if mid is not None:
            if direction == "long":
                live_pnl = round((mid - entry) / PIP, 1)
            else:
                live_pnl = round((entry - mid) / PIP, 1)
        else:
            live_pnl = None

        trade_contexts.append({
            # Identity
            "id":          trade["id"],
            "pattern":     trade["pattern"],
            "direction":   trade["direction"],
            "symbol":      trade["symbol"],

            # Levels
            "entry":       trade["entry"],
            "sl":          trade["sl"],
            "tp":          trade["tp"],
            "rr":          trade["rr"],
            "zone_top":    trade["zone_top"],
            "zone_bottom": trade["zone_bottom"],

            # Live
            "live_pnl":    live_pnl,
            "bid":         bid,
            "ask":         ask,
            "mid":         mid,

            # Context
            "confluence":  trade.get("confluence", []),
            "status":      trade["status"],
            "placed_time": trade["placed_time"],
            "signal_time": trade["signal_time"],

            # Source detail for chart drawing
            # Only the zone/level the trade depends on — nothing extra
            "chart_zones": _extract_chart_zones(trade)
        })

    # Summary
    winning  = [t for t in active_trades if (t.get("pnl_pips") or 0) > 0]
    losing   = [t for t in active_trades if (t.get("pnl_pips") or 0) < 0]

    return {
        "ts":           datetime.utcnow().isoformat(),
        "symbol":       symbol,
        "bid":          bid,
        "ask":          ask,
        "mid":          mid,
        "spread":       round((ask - bid) / PIP, 1) if ask and bid else None,
        "trades":       trade_contexts,
        "summary": {
            "total":    len(active_trades),
            "winning":  len(winning),
            "losing":   len(losing),
            "flat":     len(active_trades) - len(winning) - len(losing)
        }
    }


def _extract_chart_zones(trade):
    """
    Returns only the chart-relevant zones for this specific trade.
    The frontend draws exactly these — no extra noise.
    """
    zones = []

    # The trade zone itself (OB, FVG, etc.)
    zones.append({
        "label":     trade["pattern"],
        "direction": trade["direction"],
        "top":       trade["zone_top"],
        "bottom":    trade["zone_bottom"],
        "color":     "bull" if trade["direction"] == "long" else "bear"
    })

    # SL level
    if trade["sl"]:
        zones.append({
            "label":  "SL",
            "type":   "line",
            "price":  trade["sl"],
            "color":  "sl"
        })

    # TP level
    if trade["tp"]:
        zones.append({
            "label":  "TP",
            "type":   "line",
            "price":  trade["tp"],
            "color":  "tp"
        })

    # Entry level
    zones.append({
        "label":  "Entry",
        "type":   "line",
        "price":  trade["entry"],
        "color":  "entry"
    })

    # Trendline — add the projected line if applicable
    if trade["pattern"] == "Trendline":
        detail = trade.get("lgn_signal", {}).get("source_detail", {})
        if detail.get("projected_y"):
            zones.append({
                "label":     "Trendline",
                "type":      "trendline",
                "projected_y": detail["projected_y"],
                "slope":     detail.get("slope"),
                "direction": detail.get("direction"),
                "color":     "trendline"
            })

    return zones


# =========================================================
# 7. MONITOR CYCLE (single pass)
# =========================================================
def monitor_cycle(active_trades, symbol):
    """
    Runs one monitoring pass over all active trades.
    Called every MONITOR_INTERVAL seconds by the async loop.

    Returns:
        updated active_trades list (closed trades removed)
        frontend_context dict (sent to WebSocket clients)
    """
    if not active_trades:
        context = build_frontend_context([], symbol)
        return [], context

    # Get latest closed 5M candle for condition checks
    candle = get_latest_5m_candle(symbol)

    if candle is None:
        print("[EC] Could not fetch 5M candle — skipping condition check")
        context = build_frontend_context(active_trades, symbol)
        return active_trades, context

    still_active = []

    for trade in active_trades:
        if trade["status"] != "active":
            continue

        closed       = False
        close_reason = None
        exit_type    = "condition"

        # ── Check SL/TP first (hard limits) ───────────────
        sl_tp_hit, exit_via, sl_tp_reason = _check_sl_tp(trade, candle)

        if sl_tp_hit:
            close_price = trade["sl"] if exit_via == "sl" else trade["tp"]
            success, mt5_price = close_mt5_position(trade)
            final_price = mt5_price or close_price
            _close_record(trade, final_price, sl_tp_reason, exit_type=exit_via)
            closed = True

        # ── Check pattern condition violation ──────────────
        if not closed:
            violated, reason = check_condition(trade, candle)

            if violated:
                success, mt5_price = close_mt5_position(trade)

                # Use MT5 close price if available, else use candle close
                final_price = mt5_price or candle["C"]
                _close_record(trade, final_price, reason, exit_type="condition")
                closed = True

        if not closed:
            still_active.append(trade)

    # Build frontend payload from remaining active trades
    context = build_frontend_context(still_active, symbol)

    closed_count = len(active_trades) - len(still_active)
    if closed_count:
        print(f"[EC] Cycle complete — {closed_count} trade(s) closed, "
              f"{len(still_active)} still active")

    return still_active, context


# =========================================================
# 8. ASYNC MONITOR LOOP (runs inside FastAPI)
# =========================================================
async def monitor_loop(trade_registry, ws_manager, symbol="EURUSD"):
    """
    Long-running async loop — runs inside the FastAPI server.

    Args:
        trade_registry : shared list of active trade records from V1
                         (passed by reference — mutations are reflected globally)
        ws_manager     : the FastAPI ConnectionManager from server.py
                         (broadcasts context to all connected frontend clients)
        symbol         : forex pair being traded

    Usage in server.py:
        from extrastriate import monitor_loop

        @app.on_event("startup")
        async def startup():
            asyncio.create_task(
                monitor_loop(trade_registry, manager, symbol="EURUSD")
            )
    """
    print(f"[EC] Monitor loop started — checking every {MONITOR_INTERVAL}s")

    while True:
        try:
            active, context = monitor_cycle(trade_registry, symbol)

            # Update the shared registry in place
            trade_registry.clear()
            trade_registry.extend(active)

            # Broadcast to frontend if clients are connected
            if ws_manager.active:
                await ws_manager.broadcast(context)

        except Exception as e:
            print(f"[EC] Monitor loop error: {e}")

        await asyncio.sleep(MONITOR_INTERVAL)


# =========================================================
# 9. TRADE REGISTRY HELPERS
# =========================================================
def register_trades(trade_registry, v1_records):
    """
    Adds newly placed V1 trades to the shared registry.
    Only adds trades that were actually placed (not filtered/rejected).
    """
    added = 0
    for record in v1_records:
        if record["placed"] and record["status"] == "active":
            # Avoid duplicates
            existing_ids = {t["id"] for t in trade_registry}
            if record["id"] not in existing_ids:
                trade_registry.append(record)
                added += 1
                print(f"[EC] Registered trade: {record['id']}")

    return added


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from retina import run_retina
    from lgn    import run_lgn
    from v1     import run_v1

    print("=" * 65)
    print("RETINA (4H)...")
    retina_result = run_retina()

    print("=" * 65)
    print("LGN (5M)...")
    lgn_signals = run_lgn(retina_result)

    print("=" * 65)
    print("V1 (Execution)...")
    trades = run_v1(lgn_signals, retina_result)

    print("=" * 65)
    print("EXTRASTRIATE CORTEX (Monitor cycle)...")

    # Simulate registry with placed trades
    registry = [t for t in trades if t["placed"]]
    print(f"  Active trades in registry: {len(registry)}")

    # Run one monitor cycle
    remaining, context = monitor_cycle(registry, symbol="EURUSD")

    print("=" * 65)
    print(f"Context payload for frontend:")
    print(f"  Symbol  : {context['symbol']}")
    print(f"  Bid/Ask : {context['bid']} / {context['ask']}")
    print(f"  Trades  : {context['summary']['total']}")
    print(f"  Winning : {context['summary']['winning']}")
    print(f"  Losing  : {context['summary']['losing']}")

    for t in context["trades"]:
        print(f"\n  [{t['direction'].upper()}] {t['pattern']}")
        print(f"    Entry    : {t['entry']:.5f}")
        print(f"    SL / TP  : {t['sl']:.5f} / {t['tp']}")
        print(f"    Live PnL : {t['live_pnl']} pips")
        print(f"    Zones    : {len(t['chart_zones'])} chart elements")
