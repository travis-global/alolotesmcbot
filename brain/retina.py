import random
from datetime import datetime, timedelta


# =========================================================
# 1. DATA GENERATION
# =========================================================
def generate_ohlc_data(num_candles=300):
    """
    Synthetic OHLC generator for offline testing.
    Simulates realistic consolidation + impulse sequences
    so BOS/CHoCH/OB logic has enough structure to fire.
    Candle interval kept at 4H for Retina compatibility.
    """
    data       = []
    base_price = 1.2000
    time       = datetime.now()

    ob_active    = False
    ob_direction = None
    ob_high      = None
    ob_low       = None
    ob_countdown = 0

    for _ in range(num_candles):
        open_price = base_price

        if ob_active and ob_countdown > 0:
            if ob_direction == "bull":
                high_price  = min(open_price + random.uniform(0.0005, 0.0010), ob_high)
                low_price   = max(open_price - random.uniform(0.0005, 0.0010), ob_low)
                close_price = random.uniform(low_price, low_price + (high_price - low_price) * 0.5)
            else:
                high_price  = min(open_price + random.uniform(0.0005, 0.0010), ob_high)
                low_price   = max(open_price - random.uniform(0.0005, 0.0010), ob_low)
                close_price = random.uniform(low_price + (high_price - low_price) * 0.5, high_price)
            ob_countdown -= 1

        elif ob_active and ob_countdown == 0:
            if ob_direction == "bull":
                high_price  = open_price + random.uniform(0.0030, 0.0060)
                low_price   = open_price - random.uniform(0.0002, 0.0005)
                close_price = random.uniform(open_price + (high_price - open_price) * 0.6, high_price)
            else:
                high_price  = open_price + random.uniform(0.0002, 0.0005)
                low_price   = open_price - random.uniform(0.0030, 0.0060)
                close_price = random.uniform(low_price, low_price + (open_price - low_price) * 0.4)
            ob_active = False

        else:
            high_price  = open_price + random.uniform(0.0005, 0.0020)
            low_price   = open_price - random.uniform(0.0005, 0.0020)
            close_price = random.uniform(low_price, high_price)

            if random.random() < 0.12:
                ob_active    = True
                ob_direction = random.choice(["bull", "bear"])
                ob_high      = high_price
                ob_low       = low_price
                ob_countdown = random.randint(2, 5)

        data.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "O":    round(open_price,  5),
            "H":    round(high_price,  5),
            "L":    round(low_price,   5),
            "C":    round(close_price, 5)
        })

        base_price  = close_price
        time       += timedelta(hours=1)   # H1 candle spacing matches Retina timeframe

    return data


def generate_ohlc_data_live(symbol="EURUSD", timeframe_str="H1", num_candles=500):
    """
    Fetch live candles from Deriv API.
    timeframe_str: "H1" for Retina (4H), "M5" for LGN (5M).
    Falls back to synthetic data if Deriv is unavailable.
    """
    from utils.deriv_client import fetch_candles
    data = fetch_candles(symbol, timeframe=timeframe_str, count=num_candles)
    if not data:
        print(f"[Retina] Deriv returned no candles for {symbol} — using synthetic data")
        return generate_ohlc_data(num_candles)
    return data



# =========================================================
# 2. FRACTAL SWING DETECTION
# =========================================================
def detect_fractal_swings(data, window=2):
    """
    Identifies swing highs (SH) and swing lows (SL) using
    a fractal window. Consecutive same-type swings are merged,
    keeping only the most extreme (highest SH, lowest SL).
    window=2 is appropriate for 4H Retina use.
    """
    raw_swings = []

    for i in range(window, len(data) - window):
        curr = data[i]

        left_highs  = [data[i - j]["H"] for j in range(1, window + 1)]
        right_highs = [data[i + j]["H"] for j in range(1, window + 1)]
        left_lows   = [data[i - j]["L"] for j in range(1, window + 1)]
        right_lows  = [data[i + j]["L"] for j in range(1, window + 1)]

        is_sh = curr["H"] > max(left_highs + right_highs)
        is_sl = curr["L"] < min(left_lows  + right_lows)

        if is_sh:
            raw_swings.append({
                "type":  "SH",
                "price": curr["H"],
                "index": i,
                "time":  curr["time"]
            })
        elif is_sl:
            raw_swings.append({
                "type":  "SL",
                "price": curr["L"],
                "index": i,
                "time":  curr["time"]
            })

    # Alternate swings — keep only the most extreme of consecutive same-type
    alternated = []
    for swing in raw_swings:
        if not alternated:
            alternated.append(swing)
            continue
        last = alternated[-1]
        if swing["type"] == last["type"]:
            if swing["type"] == "SH" and swing["price"] > last["price"]:
                alternated[-1] = swing
            elif swing["type"] == "SL" and swing["price"] < last["price"]:
                alternated[-1] = swing
        else:
            alternated.append(swing)

    return alternated


# =========================================================
# 3. MARKET STRUCTURE (HH / HL / LH / LL)
# =========================================================
def classify_structure(swings):
    """
    Labels each swing relative to the previous swing of the
    same type: SH vs last SH → HH or LH; SL vs last SL → HL or LL.
    """
    structure = []
    last_sh   = None
    last_sl   = None

    for swing in swings:
        if swing["type"] == "SH":
            if last_sh is not None:
                label = "HH" if swing["price"] > last_sh["price"] else "LH"
                structure.append({
                    "label": label,
                    "price": swing["price"],
                    "time":  swing["time"],
                    "index": swing["index"]
                })
            last_sh = swing

        elif swing["type"] == "SL":
            if last_sl is not None:
                label = "HL" if swing["price"] > last_sl["price"] else "LL"
                structure.append({
                    "label": label,
                    "price": swing["price"],
                    "time":  swing["time"],
                    "index": swing["index"]
                })
            last_sl = swing

    return structure


# =========================================================
# 4. LIQUIDITY SWEEP DETECTION
# =========================================================
def detect_liquidity_sweeps(data, swings, bos_events=None, choch_events=None, pip_size=0.00005):
    """
    Detects liquidity sweeps: price wicks beyond a swing level
    then closes back inside it with meaningful rejection body.

    Fix 1: cooldown is per-swing (local), not global.
    Fix 3: sweep must be followed by a BOS or CHoCH in the opposing
           direction within CONFIRM_WINDOW bars. Without structural
           confirmation the sweep is marked unconfirmed — noise
           that the LGN should not act on.
    """
    CONFIRM_WINDOW = 10

    sweeps      = []
    used_levels = set()

    if bos_events is None:
        bos_events = []
    if choch_events is None:
        choch_events = []

    # Build index sets for fast confirmation lookup
    bos_up_indices   = {e["break_index"] for e in bos_events   if "UP"   in e["type"]}
    bos_down_indices = {e["break_index"] for e in bos_events   if "DOWN" in e["type"]}
    choch_up_indices = {e["break_index"] for e in choch_events if "UP"   in e["type"]}
    choch_dn_indices = {e["break_index"] for e in choch_events if "DOWN" in e["type"]}

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    for swing in swings:
        level = swing["price"]

        if level in used_levels:
            continue

        last_sweep_index = -999

        for i in range(swing["index"] + 1, len(data)):

            if i - last_sweep_index < 3:
                continue

            candle = data[i]

            if swing["type"] == "SH":
                pierce = candle["H"] - level
                if pierce >= pip_size and candle["C"] < level:

                    rejection = level - candle["C"]
                    if rejection < avg_body * 0.5:
                        continue

                    # Structural confirmation — need a BOS_DOWN or CHoCH_DOWN
                    # within CONFIRM_WINDOW bars after the sweep candle
                    confirmed = any(
                        j in bos_down_indices or j in choch_dn_indices
                        for j in range(i + 1, min(i + CONFIRM_WINDOW + 1, len(data)))
                    )

                    sweeps.append({
                        "type":      "Buy-side Sweep",
                        "time":      candle["time"],
                        "level":     level,
                        "index":     i,
                        "confirmed": confirmed
                    })
                    used_levels.add(level)
                    last_sweep_index = i
                    break

            elif swing["type"] == "SL":
                pierce = level - candle["L"]
                if pierce >= pip_size and candle["C"] > level:

                    rejection = candle["C"] - level
                    if rejection < avg_body * 0.5:
                        continue

                    # Structural confirmation — need a BOS_UP or CHoCH_UP
                    confirmed = any(
                        j in bos_up_indices or j in choch_up_indices
                        for j in range(i + 1, min(i + CONFIRM_WINDOW + 1, len(data)))
                    )

                    sweeps.append({
                        "type":      "Sell-side Sweep",
                        "time":      candle["time"],
                        "level":     level,
                        "index":     i,
                        "confirmed": confirmed
                    })
                    used_levels.add(level)
                    last_sweep_index = i
                    break

    return sweeps


# =========================================================
# 5. BREAK OF STRUCTURE (BOS)
# =========================================================
def detect_bos(data, swings, displacement_factor=1.2):
    """
    Detects a Break of Structure: displacement candle that
    closes beyond the previous swing high (bullish BOS) or
    swing low (bearish BOS) in the context of the current trend.

    Deduplicates by keeping the strongest body at each break index.
    """
    bos_events = []

    if len(swings) < 3:
        return bos_events

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    trend = None

    for i in range(2, len(swings)):
        prev  = swings[i - 1]
        curr  = swings[i]
        prev2 = swings[i - 2]

        # Determine trend from swing triplet
        if prev2["type"] == "SL" and prev["type"] == "SH":
            if curr["type"] == "SL" and curr["price"] > prev2["price"]:
                trend = "bullish"

        elif prev2["type"] == "SH" and prev["type"] == "SL":
            if curr["type"] == "SH" and curr["price"] < prev2["price"]:
                trend = "bearish"

        level       = prev["price"]
        level_index = prev["index"]

        for j in range(level_index + 1, len(data)):
            candle = data[j]
            body   = abs(candle["C"] - candle["O"])

            if body < avg_body * displacement_factor:
                continue

            # Bullish BOS: displacement close above a SH in a bullish trend
            if trend == "bullish" and prev["type"] == "SH":
                if candle["C"] > level:
                    bos_events.append({
                        "type":        "BOS_UP",
                        "level":       level,
                        "break_index": j,
                        "swing_index": level_index,
                        "time":        candle["time"]
                    })
                    break

            # Bearish BOS: displacement close below a SL in a bearish trend
            elif trend == "bearish" and prev["type"] == "SL":
                if candle["C"] < level:
                    bos_events.append({
                        "type":        "BOS_DOWN",
                        "level":       level,
                        "break_index": j,
                        "swing_index": level_index,
                        "time":        candle["time"]
                    })
                    break

    # Deduplicate — keep strongest body per break_index
    unique = {}
    for b in bos_events:
        idx = b["break_index"]
        if idx not in unique:
            unique[idx] = b
        else:
            existing      = unique[idx]
            b_body        = abs(data[b["break_index"]]["C"]        - data[b["break_index"]]["O"])
            existing_body = abs(data[existing["break_index"]]["C"] - data[existing["break_index"]]["O"])
            if b_body > existing_body:
                unique[idx] = b

    return list(unique.values())


# =========================================================
# 6. CHANGE OF CHARACTER (CHoCH)
# =========================================================
def detect_choch(data, swings, displacement_factor=1.2):
    """
    Detects a Change of Character: displacement break of the
    protected level (HL in bullish, LH in bearish), signalling
    a potential trend reversal.

    After a CHoCH fires, trend resets to avoid double-labelling.
    """
    choch_events = []

    if len(swings) < 4:
        return choch_events

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    trend           = None
    protected_level = None
    protected_index = None

    for i in range(3, len(swings)):
        s1 = swings[i - 3]
        s2 = swings[i - 2]
        s3 = swings[i - 1]

        # Bullish structure: SL → SH → HL (SL where SL[1] > SL[0])
        if s1["type"] == "SL" and s2["type"] == "SH" and s3["type"] == "SL":
            if s3["price"] > s1["price"]:
                trend           = "bullish"
                protected_level = s3["price"]   # HL is the protected low
                protected_index = s3["index"]

        # Bearish structure: SH → SL → LH (SH where SH[1] < SH[0])
        elif s1["type"] == "SH" and s2["type"] == "SL" and s3["type"] == "SH":
            if s3["price"] < s1["price"]:
                trend           = "bearish"
                protected_level = s3["price"]   # LH is the protected high
                protected_index = s3["index"]

        if protected_level is None:
            continue

        for j in range(protected_index + 1, len(data)):
            candle = data[j]
            body   = abs(candle["C"] - candle["O"])

            if body < avg_body * displacement_factor:
                continue

            # Bearish CHoCH: close breaks HL in bullish structure
            if trend == "bullish" and candle["C"] < protected_level:
                choch_events.append({
                    "type":        "CHoCH_DOWN",
                    "level":       protected_level,
                    "break_index": j,
                    "swing_index": protected_index,
                    "time":        candle["time"]
                })
                trend           = "bearish"
                protected_level = None
                break

            # Bullish CHoCH: close breaks LH in bearish structure
            elif trend == "bearish" and candle["C"] > protected_level:
                choch_events.append({
                    "type":        "CHoCH_UP",
                    "level":       protected_level,
                    "break_index": j,
                    "swing_index": protected_index,
                    "time":        candle["time"]
                })
                trend           = "bullish"
                protected_level = None
                break

    return choch_events


# =========================================================
# 7. ORDER BLOCK DETECTION
# =========================================================
def detect_ob(data, bos_events, choch_events, displacement_factor=1.5, lookback=30):
    """
    Detects order blocks anchored to confirmed BOS/CHoCH events.

    Principle:
      A BOS or CHoCH already proves displacement occurred.
      Walk back from the break candle, find the FIRST (closest)
      strong candle — that is the impulse origin. Then walk back
      one more step to find the last opposing candle: that is the OB.

    Key fixes vs previous version:
      1. impulse_start breaks on the FIRST hit (closest to break),
         not the earliest hit (furthest from break).
      2. FVG is NOT a prerequisite for an OB. It is recorded as
         confluence metadata only.
      3. OB type strings are "Bullish OB" / "Bearish OB" — consistent
         with all downstream consumers (bb, pois, pd_arrays).
      4. Mitigation uses the 50% midpoint of the OB body.
      5. Opposite BOS/CHoCH after the break invalidates the OB.
    """
    order_blocks = []

    if not data:
        return order_blocks

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    events = sorted(bos_events + choch_events, key=lambda x: x["break_index"])

    for event in events:
        break_idx = event["break_index"]
        e_type    = event["type"]

        if break_idx >= len(data):
            continue

        direction = "bullish" if e_type in ("BOS_UP", "CHoCH_UP") else "bearish"

        # -------------------------------------------------------
        # 1. Find the impulse origin: the CLOSEST strong candle
        #    working backward from the break candle.
        #    FIX: break on first hit so we get the candle nearest
        #    the break, not the furthest one in the window.
        # -------------------------------------------------------
        impulse_start = None

        for i in range(break_idx - 1, max(0, break_idx - lookback), -1):
            body = abs(data[i]["C"] - data[i]["O"])
            if body >= avg_body * displacement_factor:
                impulse_start = i
                break   # STOP at first (closest) strong candle

        if impulse_start is None:
            continue

        # -------------------------------------------------------
        # 2. Find the OB candle: walk back from impulse_start and
        #    return the first candle that opposes the direction.
        #    For bullish: last bearish candle before the impulse.
        #    For bearish: last bullish candle before the impulse.
        #    Skip doji candles (body/range < 0.3).
        # -------------------------------------------------------
        ob_idx = None

        for i in range(impulse_start - 1, max(0, impulse_start - 10), -1):
            c    = data[i]
            body = abs(c["C"] - c["O"])
            rng  = c["H"] - c["L"]

            if rng > 0 and body / rng < 0.3:   # skip doji
                continue

            if direction == "bullish" and c["C"] < c["O"]:   # last bearish before up-impulse
                ob_idx = i
                break
            elif direction == "bearish" and c["C"] > c["O"]: # last bullish before down-impulse
                ob_idx = i
                break

        if ob_idx is None:
            continue

        ob_candle = data[ob_idx]

        # OB range — body only (wicks excluded)
        ob_high = max(ob_candle["O"], ob_candle["C"])
        ob_low  = min(ob_candle["O"], ob_candle["C"])
        ob_mid  = (ob_high + ob_low) / 2

        # -------------------------------------------------------
        # 3. FVG confluence — check inside the impulse zone.
        #    This is METADATA only, not a gate.
        # -------------------------------------------------------
        has_fvg = False

        for i in range(impulse_start + 2, break_idx + 1):
            if i >= len(data):
                break
            c0 = data[i - 2]
            c2 = data[i]
            if c2["L"] > c0["H"]:   # bullish FVG gap
                has_fvg = True
                break
            if c2["H"] < c0["L"]:   # bearish FVG gap
                has_fvg = True
                break

        # -------------------------------------------------------
        # 4. Mitigation: price touches 50% of OB body after break
        # -------------------------------------------------------
        mitigated        = False
        mitigation_index = None

        for i in range(break_idx + 1, len(data)):
            c = data[i]
            if direction == "bullish" and c["L"] <= ob_mid:
                mitigated        = True
                mitigation_index = i
                break
            elif direction == "bearish" and c["H"] >= ob_mid:
                mitigated        = True
                mitigation_index = i
                break

        # -------------------------------------------------------
        # 5. Structural invalidation — opposite BOS/CHoCH kills OB
        #    ONLY if price has physically entered the OB zone first.
        #    A structural shift elsewhere on the chart without price
        #    touching the zone does NOT invalidate it.
        # -------------------------------------------------------
        if not mitigated:
            price_entered_zone = False
            for i in range(break_idx + 1, len(data)):
                c = data[i]
                if direction == "bullish":
                    # Price entered the OB zone from above (retest)
                    if c["L"] <= ob_high and c["H"] >= ob_low:
                        price_entered_zone = True
                        break
                else:
                    # Price entered the OB zone from below (retest)
                    if c["H"] >= ob_low and c["L"] <= ob_high:
                        price_entered_zone = True
                        break

            if price_entered_zone:
                for future_event in events:
                    if future_event["break_index"] > break_idx:
                        # Guard: future event index may exceed exec_data length
                        if future_event["break_index"] >= len(data):
                            continue
                        if direction == "bullish" and future_event["type"] in ("BOS_DOWN", "CHoCH_DOWN"):
                            mitigated        = True
                            mitigation_index = future_event["break_index"]
                            break
                        elif direction == "bearish" and future_event["type"] in ("BOS_UP", "CHoCH_UP"):
                            mitigated        = True
                            mitigation_index = future_event["break_index"]
                            break

        order_blocks.append({
            "type":             "Bullish OB" if direction == "bullish" else "Bearish OB",
            "index":            ob_idx,
            "time":             ob_candle["time"],
            "top":              ob_high,
            "bottom":           ob_low,
            "mid":              ob_mid,
            "mitigated":        mitigated,
            "mitigation_index": mitigation_index,
            "break_index":      break_idx,
            "impulse_start":    impulse_start,
            "has_fvg":          has_fvg,
            "source":           e_type,
            "age":              0,
            "touch_count":      0,
            "expired_by":       None
        })

    # Deduplicate by (ob candle index, type)
    seen       = set()
    unique_obs = []
    for ob in order_blocks:
        key = (ob["index"], ob["type"])
        if key not in seen:
            seen.add(key)
            unique_obs.append(ob)

    return unique_obs


# =========================================================
# 8. FAIR VALUE GAP (FVG)
# =========================================================
def detect_fvg(data, bos_events, choch_events, displacement_factor=1.5):
    """
    Detects 3-candle Fair Value Gaps anchored to BOS/CHoCH.

    FIX: The displacement check now verifies the MIDDLE candle
         (the gap candle) is directionally aligned with the FVG type.
         A bearish middle candle cannot validate a bullish FVG.
    """
    fvg_list = []

    if len(data) < 3:
        return fvg_list

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    # Map structure events by their break_index for O(1) lookup
    # Widen to ±2 bars: on 4H, the BOS candle can register 1-2 bars
    # after the actual gap forms inside the impulse leg.
    structure_map = {}
    for e in bos_events + choch_events:
        for offset in range(-2, 3):   # -2, -1, 0, +1, +2
            idx = e["break_index"] + offset
            if idx < 0 or idx >= len(data):   # guard both ends
                continue
            if idx not in structure_map:
                structure_map[idx] = []
            if e["type"] not in structure_map[idx]:
                structure_map[idx].append(e["type"])

    for i in range(1, len(data) - 1):
        c1 = data[i - 1]
        c2 = data[i]
        c3 = data[i + 1]

        body2 = abs(c2["C"] - c2["O"])

        # FVG requires a displaced middle candle
        if body2 < avg_body * displacement_factor:
            continue

        # Middle candle must coincide with a structure event
        if i not in structure_map:
            continue

        event_types = structure_map[i]

        fvg_type = None

        # Bullish FVG: gap between c1 high and c3 low, middle candle is bullish
        if c3["L"] > c1["H"]:
            if c2["C"] > c2["O"]:   # FIX: middle candle must be bullish
                if any(t in ("BOS_UP", "CHoCH_UP") for t in event_types):
                    fvg_type = "FVG_BULLISH"

        # Bearish FVG: gap between c3 high and c1 low, middle candle is bearish
        elif c3["H"] < c1["L"]:
            if c2["C"] < c2["O"]:   # FIX: middle candle must be bearish
                if any(t in ("BOS_DOWN", "CHoCH_DOWN") for t in event_types):
                    fvg_type = "FVG_BEARISH"

        if fvg_type is None:
            continue

        if fvg_type == "FVG_BULLISH":
            gap_high = c3["L"]
            gap_low  = c1["H"]
        else:
            gap_high = c1["L"]
            gap_low  = c3["H"]

        gap_mid = (gap_high + gap_low) / 2

        # Mitigation: price touches the 50% midpoint of the gap
        mitigated        = False
        mitigation_index = None

        for j in range(i + 1, len(data)):
            c = data[j]
            if fvg_type == "FVG_BULLISH":
                if c["L"] <= gap_mid:
                    mitigated        = True
                    mitigation_index = j
                    break
            else:
                if c["H"] >= gap_mid:
                    mitigated        = True
                    mitigation_index = j
                    break

        fvg_list.append({
            "type":             fvg_type,
            "index":            i,
            "time":             c2["time"],
            "top":              gap_high,
            "bottom":           gap_low,
            "mid":              gap_mid,
            "mitigated":        mitigated,
            "mitigation_index": mitigation_index,
            "source_events":    event_types,
            "age":              0,
            "touch_count":      0,
            "expired_by":       None
        })

    return fvg_list


# =========================================================
# 9. BREAKER BLOCK DETECTION
# =========================================================
def detect_bb(data, order_blocks, choch_events,
              rejection_lookforward=11,
              rejection_body_factor=1.2,
              min_rejection_close=0.5):
    """
    Detects breaker blocks: an OB that has been broken by a CHoCH,
    then retested, with a confirmed rejection candle.

    FIX: OB type strings now consistently use "Bullish OB" / "Bearish OB"
         matching the output of detect_ob.
    """
    breaker_blocks = []

    if not order_blocks or not choch_events:
        return breaker_blocks

    avg_body = sum(abs(c["C"] - c["O"]) for c in data) / len(data)

    for ob in order_blocks:
        ob_type   = ob["type"]
        ob_top    = ob["top"]
        ob_bottom = ob["bottom"]
        ob_index  = ob.get("index")

        if ob_index is None:
            continue

        # -------------------------------------------------------
        # 1. Find a CHoCH that breaks through the OB zone
        # -------------------------------------------------------
        breaking_choch = None

        for choch in choch_events:
            choch_idx = choch["break_index"]

            if choch_idx <= ob_index:
                continue

            # Guard: choch_idx may be from context (500 candles)
            # but data here is exec_data (80 candles)
            if choch_idx >= len(data):
                continue

            # Bullish OB broken by bearish CHoCH (close below OB bottom)
            if ob_type == "Bullish OB":
                if "DOWN" in choch["type"] and data[choch_idx]["C"] < ob_bottom:
                    breaking_choch = choch
                    break

            # Bearish OB broken by bullish CHoCH (close above OB top)
            elif ob_type == "Bearish OB":
                if "UP" in choch["type"] and data[choch_idx]["C"] > ob_top:
                    breaking_choch = choch
                    break

        if not breaking_choch:
            continue

        break_idx          = breaking_choch["break_index"]
        retest_index       = None
        confirmation_index = None

        # -------------------------------------------------------
        # 2. Scan forward for retest into the broken OB zone
        #    then look for a confirmed rejection
        # -------------------------------------------------------
        for i in range(break_idx + 1, len(data) - 1):
            candle = data[i]

            tapped = (
                candle["H"] >= ob_bottom and
                candle["L"] <= ob_top
            )

            if not tapped:
                continue

            retest_index = i

            # Former Bullish OB → Bearish Breaker: look for bearish rejection
            if ob_type == "Bullish OB":
                for j in range(i, min(i + rejection_lookforward, len(data))):
                    rc   = data[j]
                    body = abs(rc["C"] - rc["O"])

                    bearish_rejection = (
                        rc["C"] < rc["O"] and
                        body >= avg_body * rejection_body_factor
                    )
                    strong_close = (
                        (rc["H"] - rc["C"]) <= (rc["H"] - rc["L"]) * min_rejection_close
                    )
                    continuation = (
                        j + 1 < len(data) and
                        data[j + 1]["C"] < rc["L"]
                    )

                    if bearish_rejection and strong_close and continuation:
                        confirmation_index = j
                        break

            # Former Bearish OB → Bullish Breaker: look for bullish rejection
            elif ob_type == "Bearish OB":
                for j in range(i, min(i + rejection_lookforward, len(data))):
                    rc   = data[j]
                    body = abs(rc["C"] - rc["O"])

                    bullish_rejection = (
                        rc["C"] > rc["O"] and
                        body >= avg_body * rejection_body_factor
                    )
                    strong_close = (
                        (rc["C"] - rc["L"]) <= (rc["H"] - rc["L"]) * min_rejection_close
                    )
                    continuation = (
                        j + 1 < len(data) and
                        data[j + 1]["C"] > rc["H"]
                    )

                    if bullish_rejection and strong_close and continuation:
                        confirmation_index = j
                        break

            if confirmation_index is not None:
                bb_type = "Bearish BB" if ob_type == "Bullish OB" else "Bullish BB"

                breaker_blocks.append({
                    "type":               bb_type,
                    "origin_ob_index":    ob_index,
                    "break_index":        break_idx,
                    "retest_index":       retest_index,
                    "confirmation_index": confirmation_index,
                    "top":                ob_top,
                    "bottom":             ob_bottom,
                    "time":               data[confirmation_index]["time"],
                    "status":             "active"
                })
                break   # one breaker per OB

    return breaker_blocks


# =========================================================
# 10. PREMIUM / DISCOUNT ARRAYS
# =========================================================
def detect_pd_arrays(data, structure, order_blocks, fvgs,
                     current_trend=None, zone_tolerance=0.0010):
    """
    Defines the dealing range (HH-HL or LH-LL), splits it into
    premium / equilibrium / discount thirds, and identifies valid
    OB/FVG confluence zones inside the correct P/D zone.

    FIX: FVG proximity check now uses "top" / "bottom" keys,
         consistent with detect_fvg output.
    """
    if not structure or len(structure) < 2:
        return None

    # -------------------------------------------------------
    # 1. Determine trend
    # -------------------------------------------------------
    if current_trend is None:
        recent_labels = [s["label"] for s in structure[-4:]]
        bullish_count = recent_labels.count("HH") + recent_labels.count("HL")
        bearish_count = recent_labels.count("LH") + recent_labels.count("LL")
        trend = "bullish" if bullish_count > bearish_count else "bearish"
    else:
        trend = current_trend.lower()

    # -------------------------------------------------------
    # 2. Define dealing range
    # -------------------------------------------------------
    range_high = None
    range_low  = None

    if trend == "bullish":
        last_hh = None
        last_hl = None
        for s in reversed(structure):
            if s["label"] == "HH" and last_hh is None:
                last_hh = s
            elif s["label"] == "HL" and last_hl is None:
                last_hl = s
            if last_hh and last_hl:
                break
        if not last_hh or not last_hl:
            return None
        range_high = last_hh["price"]
        range_low  = last_hl["price"]

    else:
        last_ll = None
        last_lh = None
        for s in reversed(structure):
            if s["label"] == "LL" and last_ll is None:
                last_ll = s
            elif s["label"] == "LH" and last_lh is None:
                last_lh = s
            if last_ll and last_lh:
                break
        if not last_ll or not last_lh:
            return None
        range_high = last_lh["price"]
        range_low  = last_ll["price"]

    # -------------------------------------------------------
    # 3. Split range into thirds
    # -------------------------------------------------------
    dealing_range = range_high - range_low

    if dealing_range <= 0:
        return None

    third = dealing_range / 3

    discount    = {"bottom": range_low,              "top": range_low + third}
    equilibrium = {"bottom": range_low + third,      "top": range_low + (third * 2)}
    premium     = {"bottom": range_low + (third * 2),"top": range_high}

    # -------------------------------------------------------
    # 4. Current price zone
    # -------------------------------------------------------
    current_price = data[-1]["C"]

    if current_price <= discount["top"]:
        current_zone = "discount"
    elif current_price <= equilibrium["top"]:
        current_zone = "equilibrium"
    else:
        current_zone = "premium"

    # -------------------------------------------------------
    # 5. OB / FVG zone validation
    # -------------------------------------------------------
    valid_ob  = None
    valid_fvg = None

    if trend == "bullish" and current_zone == "discount":

        for ob in reversed(order_blocks):
            if ob["type"] != "Bullish OB":
                continue
            if ob.get("mitigated") is True:
                continue
            near = (
                abs(ob["bottom"] - current_price) <= zone_tolerance or
                abs(ob["top"]    - current_price) <= zone_tolerance or
                (ob["bottom"] <= current_price <= ob["top"])
            )
            if near:
                valid_ob = ob
                break

        for fvg in reversed(fvgs):
            if fvg["type"] != "FVG_BULLISH":
                continue
            if fvg.get("mitigated") is True:
                continue
            # FIX: use "top" / "bottom" keys
            if fvg["bottom"] <= current_price <= fvg["top"]:
                valid_fvg = fvg
                break

    elif trend == "bearish" and current_zone == "premium":

        for ob in reversed(order_blocks):
            if ob["type"] != "Bearish OB":
                continue
            if ob.get("mitigated") is True:
                continue
            near = (
                abs(ob["bottom"] - current_price) <= zone_tolerance or
                abs(ob["top"]    - current_price) <= zone_tolerance or
                (ob["bottom"] <= current_price <= ob["top"])
            )
            if near:
                valid_ob = ob
                break

        for fvg in reversed(fvgs):
            if fvg["type"] != "FVG_BEARISH":
                continue
            if fvg.get("mitigated") is True:
                continue
            # FIX: use "top" / "bottom" keys
            if fvg["bottom"] <= current_price <= fvg["top"]:
                valid_fvg = fvg
                break

    entry_valid = (valid_ob is not None or valid_fvg is not None)

    return {
        "trend":         trend,
        "range_high":    range_high,
        "range_low":     range_low,
        "premium":       premium,
        "equilibrium":   equilibrium,
        "discount":      discount,
        "current_price": current_price,
        "current_zone":  current_zone,
        "valid_ob":      valid_ob,
        "valid_fvg":     valid_fvg,
        "entry_valid":   entry_valid
    }


# =========================================================
# 11. POINTS OF INTEREST (POIs)
# =========================================================
def detect_pois(data, order_blocks, fvgs, breaker_blocks,
                bos_events, choch_events, pd_arrays,
                mitigation_threshold=0.5):
    """
    Ranks valid, unmitigated OBs in the correct P/D zone as POIs.
    Tier is determined by confluence: FVG overlap, breaker overlap,
    structural backing.

    FIX: removed dead "displacement" key check (never present on OB dict).
         OB type string checks now match "Bullish OB" / "Bearish OB".
    """
    pois = []

    if not pd_arrays:
        return pois

    trend        = pd_arrays["trend"]
    current_zone = pd_arrays["current_zone"]

    def is_mitigated(zone_top, zone_bottom, start_index):
        midpoint = zone_bottom + ((zone_top - zone_bottom) * mitigation_threshold)
        for i in range(start_index + 1, len(data)):
            candle = data[i]
            if trend == "bullish":
                if candle["L"] <= midpoint and candle["C"] < midpoint:
                    return True
            else:
                if candle["H"] >= midpoint and candle["C"] > midpoint:
                    return True
        return False

    for ob in order_blocks:
        ob_type = ob["type"]
        bullish = ob_type == "Bullish OB"
        bearish = ob_type == "Bearish OB"

        # Trend alignment
        if trend == "bullish" and not bullish:
            continue
        if trend == "bearish" and not bearish:
            continue

        # P/D zone alignment
        if trend == "bullish" and current_zone != "discount":
            continue
        if trend == "bearish" and current_zone != "premium":
            continue

        top         = ob["top"]
        bottom      = ob["bottom"]
        start_index = ob.get("index", 0)

        if is_mitigated(top, bottom, start_index):
            continue

        # Structural backing — BOS or CHoCH within 10 bars
        has_structure = False
        for b in bos_events:
            if abs(b["break_index"] - start_index) <= 10:
                has_structure = True
                break
        if not has_structure:
            for c in choch_events:
                if abs(c["break_index"] - start_index) <= 10:
                    has_structure = True
                    break

        if not has_structure:
            continue

        # FVG confluence — overlap with same-direction FVG
        matching_fvg = None
        for fvg in fvgs:
            overlap = fvg["top"] >= bottom and fvg["bottom"] <= top
            same_dir = (
                (fvg["type"] == "FVG_BULLISH" and bullish) or
                (fvg["type"] == "FVG_BEARISH" and bearish)
            )
            if overlap and same_dir and not fvg.get("mitigated"):
                matching_fvg = fvg
                break

        # Breaker confluence — overlap with a breaker block
        matching_breaker = None
        for bb in breaker_blocks:
            if bb["top"] >= bottom and bb["bottom"] <= top:
                matching_breaker = bb
                break

        # Tier ranking
        if matching_fvg and matching_breaker:
            tier = 1
        elif matching_fvg or current_zone in ("discount", "premium"):
            tier = 2
        else:
            tier = 3

        pois.append({
            "type":         "Bullish POI" if bullish else "Bearish POI",
            "tier":         tier,
            "top":          top,
            "bottom":       bottom,
            "time":         ob["time"],
            "origin_index": start_index,
            "has_fvg":      matching_fvg is not None,
            "has_breaker":  matching_breaker is not None,
            "has_structure":has_structure,
            "pd_zone":      current_zone,
            "status":       "active"
        })

    return pois


# =========================================================
# 12. DOUBLE TOP DETECTION
# =========================================================
def detect_double_tops(data, structure, bos_events, choch_events,
                       peak_tolerance_percent=0.03,
                       min_pullback_percent=0.20,
                       neckline_break_lookforward=20):
    """
    Detects double tops: two comparable swing highs separated by a
    trough, with a neckline break confirmed by a BOS_DOWN or CHoCH_DOWN.
    """
    patterns = []

    if not structure or len(structure) < 3:
        return patterns

    highs = [s for s in structure if s["label"] in ("HH", "LH")]

    if len(highs) < 2:
        return patterns

    for i in range(len(highs) - 1):
        first_peak  = highs[i]
        second_peak = highs[i + 1]

        fp_price = first_peak["price"]
        sp_price = second_peak["price"]
        fp_index = first_peak["index"]
        sp_index = second_peak["index"]

        if sp_index <= fp_index:
            continue

        # Peak similarity
        peak_diff    = abs(fp_price - sp_price)
        avg_peak     = (fp_price + sp_price) / 2
        peak_percent = peak_diff / avg_peak

        if peak_percent > peak_tolerance_percent:
            continue

        # Find trough between peaks
        trough_price = None
        trough_index = None

        for j in range(fp_index + 1, sp_index):
            low = data[j]["L"]
            if trough_price is None or low < trough_price:
                trough_price = low
                trough_index = j

        if trough_price is None:
            continue

        # Pullback validation
        pullback_size    = fp_price - trough_price
        pullback_percent = pullback_size / fp_price

        if pullback_percent < min_pullback_percent:
            continue

        # Neckline break — close below trough
        break_index = None
        for j in range(sp_index + 1,
                       min(sp_index + neckline_break_lookforward, len(data))):
            if data[j]["C"] < trough_price:
                break_index = j
                break

        if break_index is None:
            continue

        # Structure confirmation (CHoCH preferred, then BOS)
        # Fix 4: ±3 bar tolerance — exact index match is too strict on 4H live data
        confirmation_type = None
        TOLERANCE = 3

        for choch in choch_events:
            if abs(choch["break_index"] - break_index) <= TOLERANCE and "DOWN" in choch["type"]:
                confirmation_type = "CHoCH"
                break

        if confirmation_type is None:
            for bos in bos_events:
                if abs(bos["break_index"] - break_index) <= TOLERANCE and "DOWN" in bos["type"]:
                    confirmation_type = "BOS"
                    break

        patterns.append({
            "type":               "Double Top",
            "first_peak_index":   fp_index,
            "second_peak_index":  sp_index,
            "neckline_index":     trough_index,
            "break_index":        break_index,
            "first_peak_price":   fp_price,
            "second_peak_price":  sp_price,
            "neckline":           trough_price,
            "confirmed":          confirmation_type is not None,
            "confirmation_type":  confirmation_type,
            "time":               data[break_index]["time"]
        })

    return patterns


# =========================================================
# 13. DOUBLE BOTTOM DETECTION
# =========================================================
def detect_double_bottoms(data, structure, bos_events, choch_events,
                          trough_tolerance_percent=0.03,
                          min_rally_percent=0.20,
                          neckline_break_lookforward=20):
    """
    Detects double bottoms: two comparable swing lows separated by a
    peak, with a neckline break confirmed by a BOS_UP or CHoCH_UP.
    """
    patterns = []

    if not structure or len(structure) < 3:
        return patterns

    lows = [s for s in structure if s["label"] in ("LL", "HL")]

    if len(lows) < 2:
        return patterns

    for i in range(len(lows) - 1):
        first_trough  = lows[i]
        second_trough = lows[i + 1]

        ft_price = first_trough["price"]
        st_price = second_trough["price"]
        ft_index = first_trough["index"]
        st_index = second_trough["index"]

        if st_index <= ft_index:
            continue

        # Trough similarity
        trough_diff    = abs(ft_price - st_price)
        avg_trough     = (ft_price + st_price) / 2
        trough_percent = trough_diff / avg_trough

        if trough_percent > trough_tolerance_percent:
            continue

        # Find peak between troughs
        peak_price = None
        peak_index = None

        for j in range(ft_index + 1, st_index):
            high = data[j]["H"]
            if peak_price is None or high > peak_price:
                peak_price = high
                peak_index = j

        if peak_price is None:
            continue

        # Rally validation
        rally_size    = peak_price - ft_price
        rally_percent = rally_size / ft_price

        if rally_percent < min_rally_percent:
            continue

        # Neckline break — close above peak
        break_index = None
        for j in range(st_index + 1,
                       min(st_index + neckline_break_lookforward, len(data))):
            if data[j]["C"] > peak_price:
                break_index = j
                break

        if break_index is None:
            continue

        # Structure confirmation (CHoCH preferred, then BOS)
        # Fix 4: ±3 bar tolerance
        confirmation_type = None
        TOLERANCE = 3

        for choch in choch_events:
            if abs(choch["break_index"] - break_index) <= TOLERANCE and "UP" in choch["type"]:
                confirmation_type = "CHoCH"
                break

        if confirmation_type is None:
            for bos in bos_events:
                if abs(bos["break_index"] - break_index) <= TOLERANCE and "UP" in bos["type"]:
                    confirmation_type = "BOS"
                    break

        patterns.append({
            "type":                "Double Bottom",
            "first_trough_index":  ft_index,
            "second_trough_index": st_index,
            "neckline_index":      peak_index,
            "break_index":         break_index,
            "first_trough_price":  ft_price,
            "second_trough_price": st_price,
            "neckline":            peak_price,
            "confirmed":           confirmation_type is not None,
            "confirmation_type":   confirmation_type,
            "time":                data[break_index]["time"]
        })

    return patterns


# =========================================================
# 14. TRENDLINE DETECTION
# =========================================================
def detect_trendlines(data, swings, bos_events, choch_events,
                      min_touches=2, max_gap=80,
                      slope_tolerance=0.0002, touch_tolerance_pips=0.0005,
                      min_candles_between_touches=3):
    """
    Detects valid 4H trendlines from fractal swings.

    Principle:
      - Ascending trendline  : connects higher swing lows  → bullish bias
      - Descending trendline : connects lower swing highs  → bearish bias

    A line is defined by any 2 swings of the same type.
    It is valid if:
      1. At least min_touches (default 2) swings lie on or near the line
         within touch_tolerance_pips.
      2. The line has not been broken by a displacement candle closing
         clearly beyond it.
      3. The slope is meaningful (not flat — filtered by slope_tolerance).
      4. Swings used are within max_gap candles of each other.

    Output fields:
      - "state": "watching" (2 touches, waiting for 3rd)
                 "third_touch_confirmed" (3+ touches, LGN trigger active)
                 "broken" (price closed through line with displacement)
      - "touch_count"   : how many swings have confirmed the line
      - "last_touch_index" : index of the most recent touch
      - "projected_y"  : where the line sits at the last candle (current value)
      - "slope"        : price change per candle
      - "direction"    : "ascending" or "descending"
      - "trade_bias"   : "long" or "short"
    """
    trendlines = []

    if not data or len(swings) < 2:
        return trendlines

    bodies   = [abs(c["C"] - c["O"]) for c in data]
    avg_body = sum(bodies) / len(bodies)

    sh_swings = [s for s in swings if s["type"] == "SH"]
    sl_swings = [s for s in swings if s["type"] == "SL"]

    def build_lines(swing_list, line_type):
        """
        For each pair of swings, define a line and score it.
        line_type: "ascending" (SL swings) or "descending" (SH swings)
        """
        lines = []

        for i in range(len(swing_list) - 1):
            s1 = swing_list[i]
            s2 = swing_list[i + 1]

            # Gap check — anchors must be within max_gap candles
            if s2["index"] - s1["index"] > max_gap:
                continue

            # Slope
            dx    = s2["index"] - s1["index"]
            dy    = s2["price"] - s1["price"]
            slope = dy / dx if dx != 0 else 0

            # Filter flat lines
            if abs(slope) < slope_tolerance / max(dx, 1):
                continue

            # Direction check
            if line_type == "ascending" and slope <= 0:
                continue
            if line_type == "descending" and slope >= 0:
                continue

            # Project the line value at any index
            def line_at(idx):
                return s1["price"] + slope * (idx - s1["index"])

            # Count all swings that lie on this line within tolerance
            touches     = []
            touch_count = 0

            for s in swing_list:
                if s["index"] < s1["index"]:
                    continue
                projected = line_at(s["index"])
                delta     = abs(s["price"] - projected)
                if delta <= touch_tolerance_pips:
                    # Enforce minimum candles between consecutive touches
                    if touches and s["index"] - touches[-1]["index"] < min_candles_between_touches:
                        continue
                    touches.append(s)
                    touch_count += 1

            if touch_count < min_touches:
                continue

            last_touch  = touches[-1]
            last_idx    = len(data) - 1
            projected_y = line_at(last_idx)

            # ── Broken line check ──────────────────────────────
            # A line is broken when a displacement candle closes
            # clearly beyond it after the last touch.
            broken      = False
            broken_idx  = None

            for j in range(last_touch["index"] + 1, len(data)):
                c    = data[j]
                body = abs(c["C"] - c["O"])
                lvl  = line_at(j)

                if line_type == "ascending":
                    # Broken if close drops decisively below the line
                    if c["C"] < lvl - touch_tolerance_pips and body >= avg_body * 1.2:
                        broken     = True
                        broken_idx = j
                        break
                else:
                    # Broken if close pushes decisively above the line
                    if c["C"] > lvl + touch_tolerance_pips and body >= avg_body * 1.2:
                        broken     = True
                        broken_idx = j
                        break

            # State
            if broken:
                state = "broken"
            elif touch_count >= 3:
                state = "third_touch_confirmed"
            else:
                state = "watching"   # 2 touches — valid line, waiting for 3rd

            trade_bias = "long" if line_type == "ascending" else "short"

            lines.append({
                "type":              "Trendline",
                "direction":         line_type,
                "trade_bias":        trade_bias,
                "state":             state,
                "touch_count":       touch_count,
                "touches":           touches,
                "anchor_1":          s1,
                "anchor_2":          s2,
                "slope":             slope,
                "projected_y":       projected_y,
                "last_touch_index":  last_touch["index"],
                "last_touch_time":   last_touch["time"],
                "broken":            broken,
                "broken_index":      broken_idx,
                "time":              s2["time"]   # line established at 2nd touch
            })

        return lines

    asc_lines  = build_lines(sl_swings, "ascending")
    desc_lines = build_lines(sh_swings, "descending")

    all_lines = asc_lines + desc_lines

    # ── Deduplicate overlapping lines ─────────────────────────
    # Keep the line with the most touches when two lines share
    # the same anchor_1 swing.
    seen_anchor = {}
    for line in sorted(all_lines, key=lambda x: -x["touch_count"]):
        anchor_key = (line["anchor_1"]["index"], line["direction"])
        if anchor_key not in seen_anchor:
            seen_anchor[anchor_key] = line

    trendlines = list(seen_anchor.values())

    # Sort by touch count descending — strongest lines first
    trendlines.sort(key=lambda x: -x["touch_count"])

    return trendlines


# =========================================================
# 15. FULL RETINA PIPELINE
# =========================================================
def run_retina(data=None, symbol="EURUSD"):
    """
    Full Retina pipeline. Pass real OHLC data (list of dicts with
    keys O, H, L, C, time) or leave None to run on synthetic data.

    For live use:
        data = generate_ohlc_data_live(symbol="EURUSD", timeframe_str="H1")
        result = run_retina(data)

    Lookback windows
    ─────────────────────────────────────────────────────────
    LOOKBACK_CONTEXT   : candles used for structure, BOS, CHoCH,
                         swings — needs deep history for macro bias.
    LOOKBACK_EXECUTION : candles used for OBs, FVGs, sweeps, POIs
                         — only the freshest imbalances matter here.

    Expiration rules (applied post-detection)
    ─────────────────────────────────────────────────────────
    OB_MAX_AGE      : invalidate OB if it is older than this many
                      candles since it formed (break_index - ob_index).
    OB_MAX_TOUCHES  : invalidate OB after this many price entries
                      into the zone (each touch weakens the block).
    FVG_MAX_AGE     : same concept for FVGs.
    FVG_MAX_TOUCHES : FVGs are single-use by nature; default 1.
    """

    # ── Lookback windows ──────────────────────────────────
    LOOKBACK_CONTEXT   = 500
    LOOKBACK_EXECUTION = 80

    # ── Expiration thresholds ─────────────────────────────
    OB_MAX_AGE      = 120   # candles
    OB_MAX_TOUCHES  = 2
    FVG_MAX_AGE     = 80    # candles (FVGs lose relevance faster)
    FVG_MAX_TOUCHES = 1

    if data is None:
        data = generate_ohlc_data_live(symbol=symbol, timeframe_str="H1",
                                       num_candles=LOOKBACK_CONTEXT)
        if not data:
            print(f"[Retina] Falling back to synthetic data for {symbol}")
            data = generate_ohlc_data(LOOKBACK_CONTEXT)

    # Full dataset for context-level detection
    ctx  = data
    # Sliced dataset for execution-level detection
    exec_data = data[-LOOKBACK_EXECUTION:]

    # ── Context layer ─────────────────────────────────────
    swings       = detect_fractal_swings(ctx)
    structure    = classify_structure(swings)
    bos_events   = detect_bos(ctx, swings)
    choch_events = detect_choch(ctx, swings)

    # ── Execution layer ───────────────────────────────────
    # Swings, sweeps, OBs, FVGs all use the execution slice.
    # BOS/CHoCH passed in come from context for sweep confirmation.
    exec_swings = detect_fractal_swings(exec_data)
    sweeps      = detect_liquidity_sweeps(exec_data, exec_swings, bos_events, choch_events)
    order_blocks = detect_ob(exec_data, bos_events, choch_events)
    fvgs         = detect_fvg(exec_data, bos_events, choch_events)

    # ── Apply OB expiration ───────────────────────────────
    current_idx = len(exec_data) - 1

    for ob in order_blocks:
        if ob["mitigated"]:
            continue

        age = current_idx - ob.get("index", 0)
        if age > OB_MAX_AGE:
            ob["mitigated"]  = True
            ob["expired_by"] = "age"
            continue

        # Count touches: candles that entered the OB zone after break
        touch_count = 0
        for i in range(ob.get("break_index", 0) + 1, len(exec_data)):
            c = exec_data[i]
            entered = c["L"] <= ob["top"] and c["H"] >= ob["bottom"]
            if entered:
                touch_count += 1
            if touch_count >= OB_MAX_TOUCHES:
                ob["mitigated"]  = True
                ob["expired_by"] = "touches"
                break

        ob["age"]         = age
        ob["touch_count"] = touch_count

    # ── Apply FVG expiration ──────────────────────────────
    for fvg in fvgs:
        if fvg["mitigated"]:
            continue

        age = current_idx - fvg.get("index", 0)
        if age > FVG_MAX_AGE:
            fvg["mitigated"]  = True
            fvg["expired_by"] = "age"
            continue

        touch_count = 0
        for i in range(fvg.get("index", 0) + 1, len(exec_data)):
            c = exec_data[i]
            entered = c["L"] <= fvg["top"] and c["H"] >= fvg["bottom"]
            if entered:
                touch_count += 1
            if touch_count >= FVG_MAX_TOUCHES:
                fvg["mitigated"]  = True
                fvg["expired_by"] = "touches"
                break

        fvg["age"]         = age
        fvg["touch_count"] = touch_count

    # ── Remaining pipeline (uses exec_data / context as needed) ──
    breakers  = detect_bb(exec_data, order_blocks, choch_events)
    pd_arrays = detect_pd_arrays(exec_data, structure, order_blocks, fvgs)
    pois      = detect_pois(
                    exec_data, order_blocks, fvgs, breakers,
                    bos_events, choch_events, pd_arrays
                )
    double_tops    = detect_double_tops(ctx, structure, bos_events, choch_events)
    double_bottoms = detect_double_bottoms(ctx, structure, bos_events, choch_events)
    trendlines     = detect_trendlines(ctx, swings, bos_events, choch_events)

    return {
        "data":           data,
        "exec_data":      exec_data,
        "swings":         swings,
        "structure":      structure,
        "sweeps":         sweeps,
        "bos_events":     bos_events,
        "choch_events":   choch_events,
        "order_blocks":   order_blocks,
        "fvgs":           fvgs,
        "breakers":       breakers,
        "pd_arrays":      pd_arrays,
        "pois":           pois,
        "double_tops":    double_tops,
        "double_bottoms": double_bottoms,
        "trendlines":     trendlines
    }


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    result = run_retina()

    print(f"Candles      : {len(result['data'])}")
    print(f"Swings       : {len(result['swings'])}")
    print(f"Structure    : {len(result['structure'])}")
    print(f"Sweeps       : {len(result['sweeps'])}")
    print(f"BOS events   : {len(result['bos_events'])}")
    print(f"CHoCH events : {len(result['choch_events'])}")
    print(f"Order blocks : {len(result['order_blocks'])}")
    print(f"FVGs         : {len(result['fvgs'])}")
    print(f"Breakers     : {len(result['breakers'])}")
    print(f"POIs         : {len(result['pois'])}")
    print(f"Double tops  : {len(result['double_tops'])}")
    print(f"Double bots  : {len(result['double_bottoms'])}")
    print(f"Trendlines   : {len(result['trendlines'])}")

    watching   = [t for t in result['trendlines'] if t['state'] == 'watching']
    triggered  = [t for t in result['trendlines'] if t['state'] == 'third_touch_confirmed']
    broken     = [t for t in result['trendlines'] if t['state'] == 'broken']
    print(f"  Watching   : {len(watching)}")
    print(f"  Triggered  : {len(triggered)}")
    print(f"  Broken     : {len(broken)}")

    if result["pd_arrays"]:
        pd = result["pd_arrays"]
        print(f"\nP/D Arrays   : trend={pd['trend']}, zone={pd['current_zone']}, entry_valid={pd['entry_valid']}")
