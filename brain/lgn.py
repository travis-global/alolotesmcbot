"""
LGN — Pattern Confirmation Engine
===================================
Operates on 5M candles.
Receives the full Retina (4H) output and confirms each detected
pattern individually before passing a signal to V1.

Confirmation rule (applies to ALL 7 patterns):
  Price must arrive at the pattern zone AND show measurable intent
  by closing a 5M candle at least ENTRY_BUFFER pips in the trade
  direction before a signal is emitted.

SL and TP are NOT set here — that is V1's responsibility.
Conflicting signals (e.g. long OB + short Double Top) are both
emitted independently. V1 decides how to handle them.
"""

import random
from datetime import datetime, timedelta


# =========================================================
# CONFIG
# =========================================================
ENTRY_BUFFER      = 0.00030   # 3 pips — minimum confirming move before signal fires
                               # (was 1.5 pips — too tight, fired on noise)
ZONE_PROXIMITY    = 0.00050   # 5 pips — how close price must be to a zone
TRENDLINE_PROX    = 0.00030   # 3 pips — proximity to trendline projected_y
M5_LOOKBACK       = 200       # number of 5M candles to fetch

# How many recent 5M candles LGN scans for zone confirmation.
# 20 candles = 100 minutes of lookback — too stale.
# 5 candles = 25 minutes — only recent touches count.
CONFIRM_CANDLES   = 5


# =========================================================
# 1. 5M DATA FEED
# =========================================================
def fetch_5m_data(symbol="EURUSD", num_candles=M5_LOOKBACK):
    """
    Fetches live 5M candles from Deriv API.
    Falls back to synthetic data if Deriv is unavailable.
    """
    try:
        from utils.deriv_client import fetch_candles
        data = fetch_candles(symbol, timeframe="M5", count=num_candles)
        if not data:
            raise RuntimeError("No 5M data returned from Deriv")
        return data
    except Exception as e:
        print(f"[LGN] Deriv unavailable ({e}), using synthetic 5M data")
        return _synthetic_5m(num_candles)



def _synthetic_5m(num_candles=200):
    """Synthetic 5M data for offline testing."""
    data       = []
    base       = 1.2000
    time       = datetime.now()

    for _ in range(num_candles):
        o = base
        h = o + random.uniform(0.0002, 0.0010)
        l = o - random.uniform(0.0002, 0.0010)
        c = random.uniform(l, h)
        data.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "O": round(o, 5), "H": round(h, 5),
            "L": round(l, 5), "C": round(c, 5)
        })
        base  = c
        time += timedelta(minutes=5)

    return data


# =========================================================
# 2. HELPERS
# =========================================================
def _current_price(m5_data):
    """Last 5M close."""
    return m5_data[-1]["C"] if m5_data else None


def _candle_closed_above(candle, level):
    """5M candle closed above a level by at least ENTRY_BUFFER."""
    return candle["C"] > level + ENTRY_BUFFER


def _candle_closed_below(candle, level):
    """5M candle closed below a level by at least ENTRY_BUFFER."""
    return candle["C"] < level - ENTRY_BUFFER


def _price_in_zone(price, top, bottom, proximity=ZONE_PROXIMITY):
    """Price is inside or within proximity of a zone."""
    return (bottom - proximity) <= price <= (top + proximity)


def _has_5m_choch_or_bos(m5_data, direction, from_index, lookback=10):
    """
    Scans recent 5M candles for a structure shift in the given direction.
    direction: "bullish" or "bearish"
    Looks back `lookback` candles from from_index.
    Uses a simple swing-based check: a candle that closes beyond
    the highest high (bullish) or lowest low (bearish) of the
    preceding 3 candles counts as a micro BOS.
    """
    start = max(0, from_index - lookback)

    for i in range(start + 3, min(from_index + lookback, len(m5_data))):
        window = m5_data[i - 3: i]
        candle = m5_data[i]

        if direction == "bullish":
            prev_high = max(c["H"] for c in window)
            if candle["C"] > prev_high:
                return True, i

        elif direction == "bearish":
            prev_low = min(c["L"] for c in window)
            if candle["C"] < prev_low:
                return True, i

    return False, None


def _make_signal(pattern_type, trade_direction, trigger_price,
                 zone_top, zone_bottom, trigger_time,
                 confluence, source_detail):
    """Builds a standardised signal dict passed to V1."""
    return {
        "source":          "LGN",
        "pattern":         pattern_type,
        "direction":       trade_direction,     # "long" or "short"
        "trigger_price":   trigger_price,
        "zone_top":        zone_top,
        "zone_bottom":     zone_bottom,
        "time":            trigger_time,
        "confluence":      confluence,          # list of supporting factors
        "source_detail":   source_detail,       # original Retina object
        "status":          "pending"            # V1 will set to active/filled/closed
    }


# =========================================================
# 3. PATTERN CONFIRMERS
# =========================================================

# ---------------------------------------------------------
# 3a. Order Block
# ---------------------------------------------------------
def _confirm_ob(ob, m5_data):
    """
    Bullish OB  → long:
      Price enters OB zone from above, then a 5M candle closes
      ENTRY_BUFFER above OB bottom (showing support holding).

    Bearish OB → short:
      Price enters OB zone from below, then a 5M candle closes
      ENTRY_BUFFER below OB top (showing resistance holding).
    """
    if ob.get("mitigated"):
        return None

    bull      = ob["type"] == "Bullish OB"
    direction = "long" if bull else "short"
    top       = ob["top"]
    bottom    = ob["bottom"]
    price     = _current_price(m5_data)

    if price is None:
        return None

    # Is price in or near the zone?
    if not _price_in_zone(price, top, bottom):
        return None

    # Scan recent 5M candles for entry confirmation
    for i in range(len(m5_data) - 1, max(0, len(m5_data) - CONFIRM_CANDLES), -1):
        candle = m5_data[i]
        in_zone = candle["L"] <= top and candle["H"] >= bottom

        if not in_zone:
            continue

        if bull and _candle_closed_above(candle, bottom):
            return _make_signal(
                pattern_type    = "OB",
                trade_direction = "long",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bullish OB", "5M close above OB bottom"],
                source_detail   = ob
            )

        if not bull and _candle_closed_below(candle, top):
            return _make_signal(
                pattern_type    = "OB",
                trade_direction = "short",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bearish OB", "5M close below OB top"],
                source_detail   = ob
            )

    return None


# ---------------------------------------------------------
# 3b. Fair Value Gap
# ---------------------------------------------------------
def _confirm_fvg(fvg, m5_data):
    """
    Bullish FVG → long:
      Price retraces into the gap, then closes ENTRY_BUFFER
      above the FVG bottom — gap acted as support.

    Bearish FVG → short:
      Price retraces into the gap, then closes ENTRY_BUFFER
      below the FVG top — gap acted as resistance.
    """
    if fvg.get("mitigated"):
        return None

    bull      = fvg["type"] == "FVG_BULLISH"
    direction = "long" if bull else "short"
    top       = fvg["top"]
    bottom    = fvg["bottom"]
    price     = _current_price(m5_data)

    if price is None or not _price_in_zone(price, top, bottom):
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - CONFIRM_CANDLES), -1):
        candle = m5_data[i]
        in_gap = candle["L"] <= top and candle["H"] >= bottom

        if not in_gap:
            continue

        if bull and _candle_closed_above(candle, bottom):
            return _make_signal(
                pattern_type    = "FVG",
                trade_direction = "long",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bullish FVG", "5M close above FVG bottom"],
                source_detail   = fvg
            )

        if not bull and _candle_closed_below(candle, top):
            return _make_signal(
                pattern_type    = "FVG",
                trade_direction = "short",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bearish FVG", "5M close below FVG top"],
                source_detail   = fvg
            )

    return None


# ---------------------------------------------------------
# 3c. Breaker Block
# ---------------------------------------------------------
def _confirm_bb(bb, m5_data):
    """
    Bullish BB (former bearish OB broken upward) → long:
      Price retraces to the zone, closes ENTRY_BUFFER above
      the BB bottom — former resistance now acting as support.

    Bearish BB (former bullish OB broken downward) → short:
      Price retraces to zone, closes ENTRY_BUFFER below BB top.
    """
    bull      = bb["type"] == "Bullish BB"
    top       = bb["top"]
    bottom    = bb["bottom"]
    price     = _current_price(m5_data)

    if price is None or not _price_in_zone(price, top, bottom):
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - CONFIRM_CANDLES), -1):
        candle = m5_data[i]
        in_zone = candle["L"] <= top and candle["H"] >= bottom

        if not in_zone:
            continue

        if bull and _candle_closed_above(candle, bottom):
            return _make_signal(
                pattern_type    = "BB",
                trade_direction = "long",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bullish BB", "5M close above BB bottom"],
                source_detail   = bb
            )

        if not bull and _candle_closed_below(candle, top):
            return _make_signal(
                pattern_type    = "BB",
                trade_direction = "short",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = ["Bearish BB", "5M close below BB top"],
                source_detail   = bb
            )

    return None


# ---------------------------------------------------------
# 3d. Point of Interest
# ---------------------------------------------------------
def _confirm_poi(poi, m5_data):
    """
    POI is a confluence zone — confirming it is the same as
    confirming the underlying OB/FVG inside it, but with the
    added weight of the full POI context (P/D zone, tier).

    Bullish POI → long:  5M close ENTRY_BUFFER above POI bottom.
    Bearish POI → short: 5M close ENTRY_BUFFER below POI top.
    """
    bull      = poi["type"] == "Bullish POI"
    top       = poi["top"]
    bottom    = poi["bottom"]
    price     = _current_price(m5_data)

    if price is None or not _price_in_zone(price, top, bottom):
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - CONFIRM_CANDLES), -1):
        candle = m5_data[i]
        in_zone = candle["L"] <= top and candle["H"] >= bottom

        if not in_zone:
            continue

        if bull and _candle_closed_above(candle, bottom):
            confluence = ["Bullish POI", f"Tier {poi['tier']}",
                          "5M close above POI bottom"]
            if poi.get("has_fvg"):
                confluence.append("FVG confluence")
            if poi.get("has_breaker"):
                confluence.append("Breaker confluence")

            return _make_signal(
                pattern_type    = "POI",
                trade_direction = "long",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = confluence,
                source_detail   = poi
            )

        if not bull and _candle_closed_below(candle, top):
            confluence = ["Bearish POI", f"Tier {poi['tier']}",
                          "5M close below POI top"]
            if poi.get("has_fvg"):
                confluence.append("FVG confluence")
            if poi.get("has_breaker"):
                confluence.append("Breaker confluence")

            return _make_signal(
                pattern_type    = "POI",
                trade_direction = "short",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = confluence,
                source_detail   = poi
            )

    return None


# ---------------------------------------------------------
# 3e. Double Top
# ---------------------------------------------------------
def _confirm_double_top(dt, m5_data):
    """
    Double Top → always short.
    Signal fires when:
      1. Pattern is confirmed (CHoCH/BOS on 4H already validated by Retina)
      2. Current 5M price is near or below the neckline
      3. A 5M candle closes ENTRY_BUFFER below the neckline
         (retest rejection OR continued breakdown)
    """
    if not dt.get("confirmed"):
        return None

    neckline  = dt["neckline"]
    top       = dt["second_peak_price"]
    price     = _current_price(m5_data)

    if price is None:
        return None

    # Price should be at or below neckline
    if price > neckline + ZONE_PROXIMITY:
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - 20), -1):
        candle = m5_data[i]

        # Either breaking down through neckline or retesting it from below
        near_neckline = abs(candle["H"] - neckline) <= ZONE_PROXIMITY

        if (candle["H"] >= neckline - ZONE_PROXIMITY and
                _candle_closed_below(candle, neckline)):
            return _make_signal(
                pattern_type    = "Double Top",
                trade_direction = "short",
                trigger_price   = candle["C"],
                zone_top        = top,
                zone_bottom     = neckline,
                trigger_time    = candle["time"],
                confluence      = [
                    "Double Top confirmed",
                    f"Confirmation: {dt.get('confirmation_type', 'N/A')}",
                    "5M close below neckline"
                ],
                source_detail   = dt
            )

    return None


# ---------------------------------------------------------
# 3f. Double Bottom
# ---------------------------------------------------------
def _confirm_double_bottom(db, m5_data):
    """
    Double Bottom → always long.
    Signal fires when:
      1. Pattern confirmed by Retina
      2. Current 5M price is near or above the neckline
      3. A 5M candle closes ENTRY_BUFFER above the neckline
    """
    if not db.get("confirmed"):
        return None

    neckline  = db["neckline"]
    bottom    = db["second_trough_price"]
    price     = _current_price(m5_data)

    if price is None:
        return None

    if price < neckline - ZONE_PROXIMITY:
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - 20), -1):
        candle = m5_data[i]

        if (candle["L"] <= neckline + ZONE_PROXIMITY and
                _candle_closed_above(candle, neckline)):
            return _make_signal(
                pattern_type    = "Double Bottom",
                trade_direction = "long",
                trigger_price   = candle["C"],
                zone_top        = neckline,
                zone_bottom     = bottom,
                trigger_time    = candle["time"],
                confluence      = [
                    "Double Bottom confirmed",
                    f"Confirmation: {db.get('confirmation_type', 'N/A')}",
                    "5M close above neckline"
                ],
                source_detail   = db
            )

    return None


# ---------------------------------------------------------
# 3g. Trendline
# ---------------------------------------------------------
def _confirm_trendline(tl, m5_data):
    """
    Trendline confirmation — your rule:
      Price touches the trendline a 3rd time on 5M AND
      the touching 5M candle closes in the direction of the trend.

    Ascending trendline → long:
      5M candle touches projected_y from above (wick to line),
      closes ENTRY_BUFFER above the line.

    Descending trendline → short:
      5M candle touches projected_y from below (wick to line),
      closes ENTRY_BUFFER below the line.

    Only fires if Retina state is "third_touch_confirmed" or
    "watching" (LGN catches the 3rd touch itself on 5M).
    Broken trendlines are ignored.
    """
    if tl.get("broken"):
        return None

    if tl["state"] == "broken":
        return None

    # Only trade lines with 3+ confirmed touches.
    # A 2-touch "watching" line is just two points — not a pattern.
    # Require third_touch_confirmed for a quality signal.
    if tl["state"] != "third_touch_confirmed":
        return None

    projected_y = tl["projected_y"]
    direction   = tl["direction"]     # "ascending" or "descending"
    trade_bias  = tl["trade_bias"]    # "long" or "short"
    price       = _current_price(m5_data)

    if price is None:
        return None

    # Price must be near the trendline
    if abs(price - projected_y) > TRENDLINE_PROX:
        return None

    for i in range(len(m5_data) - 1, max(0, len(m5_data) - CONFIRM_CANDLES), -1):
        candle = m5_data[i]

        # Candle must have wicked to the trendline level
        touched_line = candle["L"] <= projected_y + TRENDLINE_PROX

        if direction == "ascending" and touched_line:
            # 5M candle close above line = bullish intent confirmed
            if _candle_closed_above(candle, projected_y):
                return _make_signal(
                    pattern_type    = "Trendline",
                    trade_direction = "long",
                    trigger_price   = candle["C"],
                    zone_top        = projected_y + TRENDLINE_PROX,
                    zone_bottom     = projected_y - TRENDLINE_PROX,
                    trigger_time    = candle["time"],
                    confluence      = [
                        "Ascending trendline",
                        f"Touch count: {tl['touch_count']}",
                        "5M close above trendline"
                    ],
                    source_detail   = tl
                )

        touched_line_desc = candle["H"] >= projected_y - TRENDLINE_PROX

        if direction == "descending" and touched_line_desc:
            # 5M candle close below line = bearish intent confirmed
            if _candle_closed_below(candle, projected_y):
                return _make_signal(
                    pattern_type    = "Trendline",
                    trade_direction = "short",
                    trigger_price   = candle["C"],
                    zone_top        = projected_y + TRENDLINE_PROX,
                    zone_bottom     = projected_y - TRENDLINE_PROX,
                    trigger_time    = candle["time"],
                    confluence      = [
                        "Descending trendline",
                        f"Touch count: {tl['touch_count']}",
                        "5M close below trendline"
                    ],
                    source_detail   = tl
                )

    return None


# =========================================================
# 4. SIGNAL DEDUPLICATION
# =========================================================
def _deduplicate(signals):
    """
    Removes duplicate signals of the same pattern type and
    direction fired within the same price zone.
    Keeps the most recent trigger per (pattern, direction, zone).
    """
    seen    = {}
    unique  = []

    for sig in reversed(signals):   # most recent first
        key = (
            sig["pattern"],
            sig["direction"],
            round(sig["zone_top"],    4),
            round(sig["zone_bottom"], 4)
        )
        if key not in seen:
            seen[key] = True
            unique.append(sig)

    return list(reversed(unique))


# =========================================================
# 5. FULL LGN PIPELINE
# =========================================================
def run_lgn(retina_result, symbol="EURUSD"):
    """
    Main LGN entry point.

    Args:
        retina_result : dict returned by retina.run_retina()
        symbol        : forex pair (passed to MT5 feed)

    Returns:
        List of confirmed signal dicts, each ready for V1.
    """
    # ── Fetch 5M data ─────────────────────────────────────
    m5_data = fetch_5m_data(symbol=symbol)

    if not m5_data:
        print("[LGN] No 5M data — aborting")
        return []

    # ── Unpack Retina output ───────────────────────────────
    obs       = retina_result.get("order_blocks",  [])
    fvgs      = retina_result.get("fvgs",          [])
    breakers  = retina_result.get("breakers",      [])
    pois      = retina_result.get("pois",          [])
    dtops     = retina_result.get("double_tops",   [])
    dbots     = retina_result.get("double_bottoms",[])
    trendlines= retina_result.get("trendlines",    [])

    signals = []

    # ── Order Blocks ───────────────────────────────────────
    for ob in obs:
        sig = _confirm_ob(ob, m5_data)
        if sig:
            signals.append(sig)

    # ── Fair Value Gaps ────────────────────────────────────
    for fvg in fvgs:
        sig = _confirm_fvg(fvg, m5_data)
        if sig:
            signals.append(sig)

    # ── Breaker Blocks ─────────────────────────────────────
    for bb in breakers:
        sig = _confirm_bb(bb, m5_data)
        if sig:
            signals.append(sig)

    # ── Points of Interest ─────────────────────────────────
    for poi in pois:
        sig = _confirm_poi(poi, m5_data)
        if sig:
            signals.append(sig)

    # ── Double Tops ────────────────────────────────────────
    for dt in dtops:
        sig = _confirm_double_top(dt, m5_data)
        if sig:
            signals.append(sig)

    # ── Double Bottoms ─────────────────────────────────────
    for db in dbots:
        sig = _confirm_double_bottom(db, m5_data)
        if sig:
            signals.append(sig)

    # ── Trendlines ─────────────────────────────────────────
    for tl in trendlines:
        sig = _confirm_trendline(tl, m5_data)
        if sig:
            signals.append(sig)

    # ── Deduplicate and return ─────────────────────────────
    signals = _deduplicate(signals)

    if signals:
        print(f"[LGN] {len(signals)} signal(s) confirmed → passing to V1")
        for s in signals:
            print(f"  [{s['direction'].upper():5}] {s['pattern']:12} "
                  f"@ {s['trigger_price']:.5f}  "
                  f"| {', '.join(s['confluence'])}")
    else:
        print("[LGN] No confirmed signals this cycle")

    return signals


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    # Offline test — runs Retina on synthetic data then feeds LGN
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from retina import run_retina

    print("=" * 55)
    print("Running Retina (4H synthetic)...")
    retina_result = run_retina()
    print(f"  OBs         : {len(retina_result['order_blocks'])}")
    print(f"  FVGs        : {len(retina_result['fvgs'])}")
    print(f"  Breakers    : {len(retina_result['breakers'])}")
    print(f"  POIs        : {len(retina_result['pois'])}")
    print(f"  Double Tops : {len(retina_result['double_tops'])}")
    print(f"  Double Bots : {len(retina_result['double_bottoms'])}")
    print(f"  Trendlines  : {len(retina_result['trendlines'])}")
    print("=" * 55)
    print("Running LGN (5M synthetic)...")
    signals = run_lgn(retina_result)
    print("=" * 55)
    print(f"Total signals passed to V1: {len(signals)}")
