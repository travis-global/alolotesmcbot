"""
bot.py — Main Entry Point
===========================
Called by GitHub Actions workflows with --mode h1 or --mode m5.

--mode h1 : Runs Retina on H1 for all symbols, updates state, generates chart
--mode m5 : Runs LGN + V1 + Extrastriate on M5 for all symbols, manages trades
"""

import argparse
import os
import sys
import traceback
from datetime import datetime

# Add project root to path so all imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.state_manager   import (load_state, save_state,
                                    update_h1_state, update_m5_state,
                                    get_symbol_retina, get_open_trades)
from utils.chart_generator import generate_chart

# =========================================================
# CONFIGURABLE SYMBOLS
# Synthetics trade 24/7. Forex trades Mon-Fri only.
# =========================================================
SYMBOLS = [
    # Forex (Mon-Fri)
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "XAUUSD",

    # Crypto (24/7)
    "BTCUSD",
    "ETHUSD",

    # Volatility indices (24/7)
    "Volatility 25 Index",
    "Volatility 50 Index",
    "Volatility 75 Index",
    "Volatility 100 Index",

    # Crash & Boom (24/7)
    "Crash 500 Index",
    "Crash 1000 Index",
    "Boom 500 Index",
    "Boom 1000 Index",

    # Other synthetics (24/7)
    "Step Index",
    "Jump 75 Index",
    "Jump 100 Index",
    "Range Break 100 Index",
]


# =========================================================
# H1 MODE — Pattern detection
# =========================================================
def run_h1_mode():
    """
    Runs for every symbol:
      1. Retina detects H1 patterns
      2. LGN filters and stages active signals
      3. State is updated and saved
      4. Chart is regenerated
    """
    from brain.retina import run_retina
    from brain.lgn    import run_lgn

    print(f"\n[H1] Starting scan — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[H1] Symbols: {len(SYMBOLS)}")

    state = load_state()

    for symbol in SYMBOLS:
        try:
            print(f"\n[H1] {symbol} — running Retina...")
            retina_result = run_retina(symbol=symbol)

            obs   = len(retina_result.get("order_blocks",  []))
            fvgs  = len(retina_result.get("fvgs",          []))
            bos   = len(retina_result.get("bos_events",    []))
            choch = len(retina_result.get("choch_events",  []))
            pois  = len(retina_result.get("pois",          []))
            tls   = len(retina_result.get("trendlines",    []))
            print(f"[H1] {symbol} — OBs:{obs} FVGs:{fvgs} BOS:{bos} "
                  f"CHoCH:{choch} POIs:{pois} TLs:{tls}")

            # LGN stages signals but doesn't place trades in H1 mode
            lgn_signals = run_lgn(retina_result, symbol=symbol)
            if lgn_signals:
                print(f"[H1] {symbol} — {len(lgn_signals)} signal(s) staged for M5")

            # Update state with this symbol's detections and signals
            state = update_h1_state(state, symbol, retina_result)

            # -------------------------------------------------------
            # FIX BUG 3: Build a stable id on every signal BEFORE
            # staging so update_m5_state deduplication actually works.
            # Without this, every H1 cycle appends duplicates because
            # sig.get("id") was always None.
            # -------------------------------------------------------
            existing_ids = {s.get("id") for s in state.get("active_signals", [])}

            for sig in lgn_signals:
                sig["symbol"]    = symbol
                sig["staged_at"] = datetime.utcnow().isoformat()
                sig["confirmed"] = False
                sig["status"]    = "pending"
                sig["id"] = (
                    f"{symbol}_{sig['pattern']}_{sig['direction']}_"
                    f"{round(sig['zone_top'], 4)}_{round(sig['zone_bottom'], 4)}"
                )
                if sig["id"] not in existing_ids:
                    state.setdefault("active_signals", []).append(sig)
                    existing_ids.add(sig["id"])

        except Exception as e:
            print(f"[H1] {symbol} error: {e}")
            traceback.print_exc()

    # Generate updated chart from state
    try:
        generate_chart(state)
        print(f"\n[H1] Chart generated → output/chart.html")
    except Exception as e:
        print(f"[H1] Chart generation error: {e}")

    save_state(state)
    print(f"\n[H1] Scan complete — state saved")


# =========================================================
# M5 MODE — Confirmation, execution and monitoring
# =========================================================
def run_m5_mode():
    """
    For each symbol with staged signals or open trades:
      1. LGN re-confirms on fresh M5 data
      2. Pending H1-staged signals (active_signals) are ALSO consumed
         so a confirmed setup is never silently dropped just because
         M5 ran when price had briefly moved away from the zone.
      3. V1 places all confirmed + staged trades
      4. Extrastriate monitors open trades and closes when conditions violated
    """
    from brain.lgn          import run_lgn
    from brain.v1           import run_v1
    from brain.extrastriate import monitor_cycle, register_trades

    # FIX BUG 2: Signals older than this are expired — the setup is gone.
    SIGNAL_MAX_AGE_HOURS = 4

    print(f"\n[M5] Starting execution — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    state = load_state()
    now   = datetime.utcnow()

    # ------------------------------------------------------------------
    # FIX BUG 1 + 2: Pre-pass over active_signals before the symbol loop.
    #
    # • Expire signals older than SIGNAL_MAX_AGE_HOURS.
    # • Build a set of already-executed signal ids so we never place
    #   the same trade a second time.
    # • Group remaining pending signals by symbol for fast lookup.
    # ------------------------------------------------------------------
    executed_ids: set  = set()
    fresh_pending: list = []

    for sig in state.get("active_signals", []):
        if sig.get("status") in ("executed", "expired"):
            executed_ids.add(sig.get("id"))
            continue

        staged_at = sig.get("staged_at")
        if staged_at:
            try:
                age = now - datetime.fromisoformat(staged_at)
                if age.total_seconds() > SIGNAL_MAX_AGE_HOURS * 3600:
                    sig["status"] = "expired"
                    print(f"[M5] Expired: {sig.get('symbol')} "
                          f"{sig.get('pattern')} {sig.get('direction')}")
                    continue
            except Exception:
                pass

        fresh_pending.append(sig)

    pending_by_symbol: dict = {}
    for sig in fresh_pending:
        pending_by_symbol.setdefault(sig.get("symbol"), []).append(sig)

    for symbol in SYMBOLS:
        try:
            # Get this symbol's stored Retina result from state
            retina_data = get_symbol_retina(state, symbol)
            if not retina_data:
                continue  # No H1 scan yet for this symbol

            # Reconstruct a minimal retina_result dict from state
            retina_result = {
                "order_blocks":  retina_data.get("order_blocks",  []),
                "fvgs":          retina_data.get("fvgs",          []),
                "breakers":      retina_data.get("breakers",      []),
                "bos_events":    retina_data.get("bos_events",    []),
                "choch_events":  retina_data.get("choch_events",  []),
                "sweeps":        retina_data.get("sweeps",        []),
                "trendlines":    retina_data.get("trendlines",    []),
                "double_tops":   retina_data.get("double_tops",   []),
                "double_bottoms":retina_data.get("double_bottoms",[]),
                "pois":          retina_data.get("pois",          []),
                "pd_arrays":     retina_data.get("pd_arrays",     {}),
                "structure":     retina_data.get("structure",     []),
                "swings":        retina_data.get("swings",        []),
                "data":          retina_data.get("ohlc",          []),
                "exec_data":     retina_data.get("ohlc",          []),
            }

            # LGN re-confirms on fresh M5 data
            lgn_signals = run_lgn(retina_result, symbol=symbol)

            # ----------------------------------------------------------
            # FIX BUG 1: Merge pending H1-staged signals with LGN output.
            #
            # H1 confirmed these on 5M during the H1 run.  M5 should
            # honour them within the freshness window (SIGNAL_MAX_AGE_HOURS)
            # rather than waiting for LGN to re-confirm at the exact same
            # price moment — which may never happen.
            #
            # Dedup rule: skip any staged signal whose zone+direction
            # already appears in the fresh LGN set (same setup confirmed
            # twice → place only once).
            # ----------------------------------------------------------
            lgn_zone_keys = {
                (s["pattern"], s["direction"],
                 round(s["zone_top"], 4), round(s["zone_bottom"], 4))
                for s in lgn_signals
            }

            staged_to_execute = []
            for ps in pending_by_symbol.get(symbol, []):
                if ps.get("id") in executed_ids:
                    continue
                zone_key = (
                    ps["pattern"], ps["direction"],
                    round(ps["zone_top"], 4), round(ps["zone_bottom"], 4)
                )
                if zone_key not in lgn_zone_keys:
                    staged_to_execute.append(ps)
                    lgn_zone_keys.add(zone_key)   # prevent double-counting

            combined_signals = lgn_signals + staged_to_execute

            # V1 places trades
            if combined_signals:
                print(f"[M5] {symbol} — {len(lgn_signals)} fresh + "
                      f"{len(staged_to_execute)} staged signal(s) → V1")
                v1_records = run_v1(combined_signals, retina_result, symbol=symbol)
            else:
                v1_records = []

            # Mark staged signals as executed/rejected to prevent re-placement
            placed_zone_keys = {
                (r["pattern"], r["direction"],
                 round(r["zone_top"], 4), round(r["zone_bottom"], 4))
                for r in v1_records if r.get("placed")
            }
            for ps in staged_to_execute:
                zone_key = (
                    ps["pattern"], ps["direction"],
                    round(ps["zone_top"], 4), round(ps["zone_bottom"], 4)
                )
                if zone_key in placed_zone_keys:
                    ps["status"]    = "executed"
                    ps["confirmed"] = True
                    executed_ids.add(ps.get("id"))
                    print(f"[M5]  ✓ Staged signal consumed: "
                          f"{ps['pattern']} {ps['direction']}")
                else:
                    # V1 filtered it (bad R:R, SL issue, etc.)
                    # Keep pending so next M5 cycle can retry within window
                    pass

            # Get current open trades for this symbol from state
            open_trades = get_open_trades(state, symbol)

            # Register newly placed trades
            register_trades(open_trades, v1_records)

            # Extrastriate monitors and closes where necessary
            remaining, context = monitor_cycle(open_trades, symbol)

            # Update state with latest trade status
            state = update_m5_state(
                state, symbol,
                new_signals  = lgn_signals,
                trade_updates= remaining
            )

        except Exception as e:
            print(f"[M5] {symbol} error: {e}")
            traceback.print_exc()

    # Persist updated signal statuses back into state.
    # expired/executed signals are kept for audit; fresh_pending reflects
    # the mutated status objects (executed flags were set in-place above).
    expired_and_done = [
        s for s in state.get("active_signals", [])
        if s.get("status") in ("executed", "expired")
           and s.get("id") in executed_ids
    ]
    state["active_signals"] = fresh_pending + expired_and_done

    save_state(state)
    print(f"\n[M5] Execution cycle complete — state saved")


# =========================================================
# ENTRY POINT
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="SMC Trading Bot")
    parser.add_argument(
        "--mode",
        choices=["h1", "m5"],
        required=True,
        help="h1 = pattern detection, m5 = confirmation + execution"
    )
    args = parser.parse_args()

    if args.mode == "h1":
        run_h1_mode()
    elif args.mode == "m5":
        run_m5_mode()


if __name__ == "__main__":
    main()
