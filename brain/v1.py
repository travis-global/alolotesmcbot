"""
V1 — Trade Execution Engine
==============================
Receives confirmed signals from LGN and:
  1. Calculates SL (SMC structure-based, zone boundary + SL_BUFFER)
  2. Calculates TP (nearest unswept swing high/low from Retina swings)
  3. Filters trades with R:R below MIN_RR (minimum 1.8)
  4. Places market order via MT5
  5. Returns full trade record regardless of MT5 availability

SL logic (SMC — zone invalidation):
  Long  → SL below zone bottom - SL_BUFFER
  Short → SL above zone top    + SL_BUFFER

TP logic (nearest unswept liquidity):
  Long  → nearest swing high above entry that hasn't been swept
  Short → nearest swing low  below entry that hasn't been swept

R:R filter: minimum 1.8 (not strictly 2.0 — trades at 1.8+ are accepted)
Entry: market price (current 5M close from LGN signal)
"""

from datetime import datetime
from utils.metrics import record_placed, record_filtered


# =========================================================
# CONFIG
# =========================================================
SL_BUFFER   = 0.00003   # 3 pips beyond zone boundary for SL
MIN_RR      = 1.8       # minimum reward:risk ratio
LOT_SIZE    = 0.01      # default lot size — adjust per risk model
MAGIC       = 20250518  # MT5 magic number (identifies V1 trades)
SLIPPAGE    = 3         # max slippage pips for market order


# =========================================================
# 1. SL CALCULATION
# =========================================================
def calculate_sl(signal):
    """
    SMC SL — placed beyond the zone that defines the trade.
    If price closes beyond this level the setup is structurally invalid.

    Long  → SL = zone_bottom - SL_BUFFER
    Short → SL = zone_top    + SL_BUFFER

    Special cases:
      Double Top  short → SL above second peak (pattern fully failed)
      Double Bottom long → SL below second trough
      Trendline         → SL is already encoded in zone_bottom/zone_top
    """
    direction = signal["direction"]
    pattern   = signal["pattern"]

    if direction == "long":
        sl = signal["zone_bottom"] - SL_BUFFER

        # Double Bottom: SL below the second trough, not just the zone
        if pattern == "Double Bottom":
            detail = signal.get("source_detail", {})
            trough = detail.get("second_trough_price")
            if trough:
                sl = trough - SL_BUFFER

    else:  # short
        sl = signal["zone_top"] + SL_BUFFER

        # Double Top: SL above the second peak
        if pattern == "Double Top":
            detail = signal.get("source_detail", {})
            peak = detail.get("second_peak_price")
            if peak:
                sl = peak + SL_BUFFER

    return round(sl, 5)


# =========================================================
# 2. TP CALCULATION
# =========================================================
def calculate_tp(signal, retina_result):
    """
    TP = nearest unswept swing high (long) or swing low (short)
    above/below the entry price.

    "Unswept" means no subsequent candle has wicked beyond that level,
    meaning liquidity is still sitting there drawing price toward it.

    Falls back to a fixed 2x SL distance if no valid swing is found.
    """
    direction    = signal["direction"]
    entry        = signal["trigger_price"]
    swings       = retina_result.get("swings", [])
    sweeps       = retina_result.get("sweeps", [])
    data         = retina_result.get("data",   [])

    # Build set of already-swept levels
    swept_levels = {round(s["level"], 5) for s in sweeps if s.get("confirmed")}

    if direction == "long":
        # Find all swing highs above entry, unswept
        candidates = [
            s for s in swings
            if s["type"] == "SH"
            and s["price"] > entry
            and round(s["price"], 5) not in swept_levels
        ]
        if candidates:
            # Nearest one (lowest price above entry)
            tp = min(candidates, key=lambda s: s["price"])["price"]
        else:
            # Fallback: 2× SL distance above entry
            sl   = calculate_sl(signal)
            risk = abs(entry - sl)
            tp   = round(entry + (risk * 2), 5)

    else:  # short
        # Find all swing lows below entry, unswept
        candidates = [
            s for s in swings
            if s["type"] == "SL"
            and s["price"] < entry
            and round(s["price"], 5) not in swept_levels
        ]
        if candidates:
            # Nearest one (highest price below entry)
            tp = max(candidates, key=lambda s: s["price"])["price"]
        else:
            # Fallback: 2× SL distance below entry
            sl   = calculate_sl(signal)
            risk = abs(entry - sl)
            tp   = round(entry - (risk * 2), 5)

    return round(tp, 5)


# =========================================================
# 3. R:R CALCULATION
# =========================================================
def calculate_rr(entry, sl, tp):
    """
    Returns the reward:risk ratio.
    Risk   = abs(entry - sl)
    Reward = abs(tp    - entry)
    """
    risk   = abs(entry - sl)
    reward = abs(tp    - entry)

    if risk == 0:
        return 0.0

    return round(reward / risk, 2)


# =========================================================
# 4. PLACE ORDER VIA DERIV
# =========================================================
def _place_mt5_order(symbol, direction, entry, sl, tp,
                     lot=LOT_SIZE, magic=MAGIC, comment="V1"):
    """
    Places a market order via Deriv API.
    Named _place_mt5_order for drop-in compatibility.
    Returns (success: bool, result_detail: dict)
    """
    try:
        from utils.deriv_client import place_order
        return place_order(symbol, direction, stake=lot)
    except Exception as e:
        return False, {"error": str(e)}


# =========================================================
# 5. BUILD TRADE RECORD
# =========================================================
def _build_trade_record(signal, sl, tp, rr, placed, mt5_result,
                        symbol, filtered_reason=None):
    """
    Builds the full trade record returned by V1 regardless of
    whether the order was placed or filtered.
    """
    return {
        # Identity
        "id":             f"V1_{signal['pattern']}_{signal['direction']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        "symbol":         symbol,
        "source":         "V1",
        "pattern":        signal["pattern"],
        "direction":      signal["direction"],

        # Prices
        "entry":          signal["trigger_price"],
        "sl":             sl,
        "tp":             tp,
        "rr":             rr,

        # Zone reference (from LGN)
        "zone_top":       signal["zone_top"],
        "zone_bottom":    signal["zone_bottom"],

        # Execution
        "placed":         placed,
        "filtered":       filtered_reason is not None,
        "filtered_reason":filtered_reason,
        "mt5_result":     mt5_result,

        # Confluence
        "confluence":     signal.get("confluence", []),

        # Timestamps
        "signal_time":    signal["time"],
        "placed_time":    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),

        # Status — EXTRASTRIATE CORTEX monitors this
        "status":         "active" if placed else "rejected",
        "close_price":    None,
        "close_time":     None,
        "close_reason":   None,
        "pnl_pips":       None,

        # Full signal for audit trail
        "lgn_signal":     signal
    }


# =========================================================
# 6. FULL V1 PIPELINE
# =========================================================
def run_v1(lgn_signals, retina_result, symbol="EURUSD"):
    """
    Main V1 entry point.

    Args:
        lgn_signals    : list of signal dicts from lgn.run_lgn()
        retina_result  : dict from retina.run_retina() — used for TP swing lookup
        symbol         : forex pair

    Returns:
        List of trade record dicts (placed + rejected, all included).
        EXTRASTRIATE CORTEX receives this list for monitoring.
    """
    trade_records = []

    if not lgn_signals:
        print("[V1] No signals received from LGN")
        return trade_records

    print(f"[V1] Processing {len(lgn_signals)} signal(s) from LGN")

    for signal in lgn_signals:
        pattern   = signal["pattern"]
        direction = signal["direction"]
        entry     = signal["trigger_price"]

        # ── 1. Calculate SL ───────────────────────────────
        sl = calculate_sl(signal)

        # Sanity check — SL must be on the correct side of entry
        if direction == "long"  and sl >= entry:
            record = _build_trade_record(
                signal, sl, None, 0, False, None, symbol,
                filtered_reason="SL above entry for long"
            )
            trade_records.append(record)
            _log_trade(record)
            continue

        if direction == "short" and sl <= entry:
            record = _build_trade_record(
                signal, sl, None, 0, False, None, symbol,
                filtered_reason="SL below entry for short"
            )
            trade_records.append(record)
            _log_trade(record)
            continue

        # ── 2. Calculate TP ───────────────────────────────
        tp = calculate_tp(signal, retina_result)

        # Sanity check — TP must be on the correct side of entry
        if direction == "long"  and tp <= entry:
            record = _build_trade_record(
                signal, sl, tp, 0, False, None, symbol,
                filtered_reason="TP below entry for long"
            )
            trade_records.append(record)
            _log_trade(record)
            continue

        if direction == "short" and tp >= entry:
            record = _build_trade_record(
                signal, sl, tp, 0, False, None, symbol,
                filtered_reason="TP above entry for short"
            )
            trade_records.append(record)
            _log_trade(record)
            continue

        # ── 3. R:R filter ─────────────────────────────────
        rr = calculate_rr(entry, sl, tp)

        if rr < MIN_RR:
            record = _build_trade_record(
                signal, sl, tp, rr, False, None, symbol,
                filtered_reason=f"R:R {rr} below minimum {MIN_RR}"
            )
            trade_records.append(record)
            _log_trade(record)
            record_filtered(signal, record["filtered_reason"])   # ← METRICS
            continue

        # ── 4. Place order ────────────────────────────────
        placed, mt5_result = _place_mt5_order(
            symbol    = symbol,
            direction = direction,
            entry     = entry,
            sl        = sl,
            tp        = tp,
            comment   = f"V1_{pattern}"
        )

        # ── 5. Build and store record ─────────────────────
        record = _build_trade_record(
            signal, sl, tp, rr, placed, mt5_result, symbol
        )
        trade_records.append(record)
        _log_trade(record)

        if placed:
            record_placed(record)   # ← METRICS

    # Summary
    placed   = [r for r in trade_records if r["placed"]]
    filtered = [r for r in trade_records if r["filtered"]]

    print(f"[V1] Done — {len(placed)} placed, {len(filtered)} filtered")
    if filtered:
        for r in filtered:
            print(f"  Filtered: {r['pattern']:12} {r['direction']:5} "
                  f"— {r['filtered_reason']}")

    return trade_records


# =========================================================
# 7. LOGGER
# =========================================================
def _log_trade(record):
    """Prints a clean one-line trade log."""
    status = "PLACED  " if record["placed"] else \
             "FILTERED" if record["filtered"] else "REJECTED"

    rr_str = f"R:R {record['rr']}" if record["rr"] else "R:R —"

    print(
        f"  [{status}] {record['pattern']:12} "
        f"{record['direction'].upper():5} "
        f"| Entry {record['entry']:.5f} "
        f"SL {record['sl']:.5f} "
        f"TP {record['tp'] if record['tp'] else '—':>10} "
        f"| {rr_str}"
    )


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from retina import run_retina
    from lgn    import run_lgn

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
    print(f"Trade records returned: {len(trades)}")
    for t in trades:
        print(f"  {t['id']}")
        print(f"    Pattern   : {t['pattern']} {t['direction'].upper()}")
        print(f"    Entry     : {t['entry']:.5f}")
        print(f"    SL        : {t['sl']:.5f}")
        print(f"    TP        : {t['tp']}")
        print(f"    R:R       : {t['rr']}")
        print(f"    Status    : {t['status']}")
        print(f"    Confluence: {', '.join(t['confluence'])}")
        print()
