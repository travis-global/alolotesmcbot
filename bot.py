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
            # It just identifies which signals are worth watching
            lgn_signals = run_lgn(retina_result, symbol=symbol)
            if lgn_signals:
                print(f"[H1] {symbol} — {len(lgn_signals)} signal(s) staged for M5")

            # Update state with this symbol's detections and signals
            state = update_h1_state(state, symbol, retina_result)

            # Store staged LGN signals in state
            for sig in lgn_signals:
                sig["symbol"]     = symbol
                sig["staged_at"]  = datetime.utcnow().isoformat()
                sig["confirmed"]  = False
                state.setdefault("active_signals", []).append(sig)

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
      1. LGN confirms signals on M5
      2. V1 places confirmed trades
      3. Extrastriate monitors open trades and closes when conditions violated
    """
    from brain.lgn          import run_lgn
    from brain.v1           import run_v1
    from brain.extrastriate import monitor_cycle, register_trades

    print(f"\n[M5] Starting execution — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    state = load_state()

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

            # V1 places trades
            if lgn_signals:
                print(f"[M5] {symbol} — {len(lgn_signals)} confirmed signal(s)")
                v1_records = run_v1(lgn_signals, retina_result, symbol=symbol)
            else:
                v1_records = []

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
