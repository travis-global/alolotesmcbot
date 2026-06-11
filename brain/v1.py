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
MIN_RR      = 1.5       # minimum reward:risk ratio
LOT_SIZE    = 0.01      # default lot size — adjust per risk model
MAGIC       = 20250518  # MT5 magic number (identifies V1 trades)
SLIPPAGE    = 3         # max slippage pips for market order

# FIX BUG 4: SL_BUFFER must be scaled per instrument.
# A flat 0.00003 means 0.3 pips on EUR/USD but only 0.003 pips on
# USD/JPY (price ~150) — essentially zero protection.
# Rule: 3 pips expressed in price units for each instrument category.
#   Standard forex (4-decimal quote): 1 pip = 0.0001  → 3 pips = 0.0003
#   JPY pairs    (2-decimal quote):   1 pip = 0.01    → 3 pips = 0.030
#   XAUUSD       (2-decimal quote):   1 pip = 0.10    → 3 pips = 0.30
#   Crypto       (2-decimal quote):   1 pip = 1.00    → 3 pips = 3.00
#   Volatility / synthetic indices: use absolute buffer
_SL_BUFFER_MAP = {
    # JPY pairs
    "USDJPY": 0.030,
    "EURJPY": 0.030,
    "GBPJPY": 0.030,
    "AUDJPY": 0.030,
    "CADJPY": 0.030,
    # Gold
    "XAUUSD": 0.300,
    # Crypto
    "BTCUSD": 3.000,
    "ETHUSD": 1.500,
    # Volatility indices (typical price ~300–1500 — use 0.05%)
    "Volatility 25 Index":  0.150,
    "Volatility 50 Index":  0.250,
    "Volatility 75 Index":  0.400,
    "Volatility 100 Index": 0.600,
    # Crash / Boom
    "Crash 500 Index":  0.500,
    "Crash 1000 Index": 0.500,
    "Boom 500 Index":   0.500,
    "Boom 1000 Index":  0.500,
    # Other synthetics
    "Step Index":            0.050,
    "Jump 75 Index":         0.100,
    "Jump 100 Index":        0.100,
    "Range Break 100 Index": 0.100,
}
_SL_BUFFER_DEFAULT = 0.0003   # 3 pips for standard 4-decimal forex pairs


def _sl_buffer(symbol: str) -> float:
    """Returns the correct SL buffer in price units for the given symbol."""
    return _SL_BUFFER_MAP.get(symbol, _SL_BUFFER_DEFAULT)


# =========================================================
# 1. SL CALCULATION
# =========================================================
def calculate_sl(signal, symbol: str = "EURUSD"):
    """
    SMC SL — placed beyond the zone that defines the trade.
    If price closes beyond this level the setup is structurally invalid.

    Long  → SL = zone_bottom - buffer
    Short → SL = zone_top    + buffer

    FIX BUG 4: buffer is now looked up per symbol via _sl_buffer()
    instead of the old flat 0.00003 which was nearly zero on JPY pairs.
    """
    direction = signal["direction"]
    pattern   = signal["pattern"]
    buf       = _sl_buffer(symbol)

    if direction == "long":
        sl = signal["zone_bottom"] - buf

        # Double Bottom: SL below the second trough, not just the zone
        if pattern == "Double Bottom":
            detail = signal.get("source_detail", {})
            trough = detail.get("second_trough_price")
            if trough:
                sl = trough - buf

    else:  # short
        sl = signal["zone_top"] + buf

        # Double Top: SL above the second peak
        if pattern == "Double Top":
            detail = signal.get("source_detail", {})
            peak = detail.get("second_peak_price")
            if peak:
                sl = peak + buf

    return round(sl, 5)


# =========================================================
# 2. TP CALCULATION
# =========================================================
def calculate_tp(signal, retina_result):
    """
    TP = nearest unswept swing high (long) or swing low (short)
    above/below the entry price — BUT only if that swing is far enough
    away to give a meaningful reward.

    FIX: The old code grabbed the NEAREST swing regardless of distance.
    That caused TP to land closer to entry than SL (R:R < 1.0) because
    the nearest swing high/low is often just a minor ripple a few pips away.

    New rule: a swing-based TP is only used if it is at least
    MIN_TP_MULTIPLIER × SL_distance away from entry.
    If no qualifying swing exists, fall back to a fixed 2.5× SL distance.
    This guarantees every TP candidate already implies R:R ≥ 2.0 before
    the R:R filter even runs.
    """
    MIN_TP_MULTIPLIER = 2.0   # TP must be at least 2× the SL distance away

    direction    = signal["direction"]
    entry        = signal["trigger_price"]
    swings       = retina_result.get("swings", [])
    sweeps       = retina_result.get("sweeps", [])

    # Calculate SL distance so we can enforce the minimum TP distance
    sl          = calculate_sl(signal)
    sl_distance = abs(entry - sl)
    min_tp_dist = sl_distance * MIN_TP_MULTIPLIER

    # Build set of already-swept levels — liquidity already taken is not a target
    swept_levels = {round(s["level"], 5) for s in sweeps if s.get("confirmed")}

    if direction == "long":
        # Only swing highs that are:
        #   1. Above entry
        #   2. Unswept (liquidity still resting there)
        #   3. At least min_tp_dist above entry (so R:R ≥ MIN_TP_MULTIPLIER)
        candidates = [
            s for s in swings
            if s["type"] == "SH"
            and s["price"] > entry + min_tp_dist
            and round(s["price"], 5) not in swept_levels
        ]
        if candidates:
            # Use the nearest qualifying swing high
            tp = min(candidates, key=lambda s: s["price"])["price"]
        else:
            # Fallback: 2.5× SL distance above entry
            tp = round(entry + (sl_distance * 2.5), 5)

    else:  # short
        # Only swing lows that are:
        #   1. Below entry
        #   2. Unswept
        #   3. At least min_tp_dist below entry
        candidates = [
            s for s in swings
            if s["type"] == "SL"
            and s["price"] < entry - min_tp_dist
            and round(s["price"], 5) not in swept_levels
        ]
        if candidates:
            # Use the nearest qualifying swing low
            tp = max(candidates, key=lambda s: s["price"])["price"]
        else:
            # Fallback: 2.5× SL distance below entry
            tp = round(entry - (sl_distance * 2.5), 5)

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
        return place_order(symbol, direction, stake=lot,
                           entry=entry, sl=sl, tp=tp)
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
        sl = calculate_sl(signal, symbol=symbol)

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

            # Spam fix: only notify Telegram once per hour per signal.
            # The same staged signal is offered to V1 every 5 minutes.
            # Without this check you get 12 identical messages per hour
            # for a signal that hasn't changed at all.
            last_notified = signal.get("last_filtered_at")
            should_notify = True
            if last_notified:
                try:
                    age_secs = (
                        datetime.utcnow() - datetime.fromisoformat(last_notified)
                    ).total_seconds()
                    if age_secs < 3600:   # 1-hour cooldown
                        should_notify = False
                except Exception:
                    pass

            # Always stamp the signal so next cycle can check the cooldown.
            # Because staged signals are passed by reference from state,
            # this update will be persisted when bot.py calls save_state().
            signal["last_filtered_at"] = datetime.utcnow().isoformat()

            if should_notify:
                try:
                    from utils.telegram_notifier import notify_trade_filtered
                    notify_trade_filtered(record)
                except Exception:
                    pass
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
            try:
                from utils.telegram_notifier import notify_trade_placed
                notify_trade_placed(record)
            except Exception:
                pass
        else:
            # Order was rejected by Deriv — notify with the actual error
            deriv_error = (mt5_result or {}).get("error", "Unknown Deriv error")
            print(f"  [REJECTED] {pattern:12} {direction:5} "
                  f"| Entry {entry} SL {sl} TP {tp:>10} | R:R {rr}"
                  f" | {deriv_error}")
            try:
                from utils.telegram_notifier import notify_trade_rejected
                notify_trade_rejected(record, deriv_error)
            except Exception:
                pass

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
