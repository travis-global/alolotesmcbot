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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.state_manager   import (load_state, save_state,
                                    update_h1_state, update_m5_state,
                                    get_symbol_retina, get_open_trades)
from utils.chart_generator import generate_chart

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "XAUUSD",
    "BTCUSD", "ETHUSD",
    "Volatility 25 Index", "Volatility 50 Index",
    "Volatility 75 Index", "Volatility 100 Index",
    "Crash 500 Index", "Crash 1000 Index",
    "Boom 500 Index",  "Boom 1000 Index",
    "Step Index", "Jump 75 Index", "Jump 100 Index",
    "Range Break 100 Index",
]


# =========================================================
# H1 MODE — Pattern detection
# =========================================================
def run_h1_mode():
    from brain.retina import run_retina
    from brain.lgn    import run_lgn
    from utils.telegram_notifier import notify_h1_complete, notify_error

    print(f"\n[H1] Starting scan — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[H1] Symbols: {len(SYMBOLS)}")

    state          = load_state()
    symbol_results = []   # collected for Telegram H1 summary

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

            lgn_signals = run_lgn(retina_result, symbol=symbol)
            if lgn_signals:
                print(f"[H1] {symbol} — {len(lgn_signals)} signal(s) staged for M5")

            state = update_h1_state(state, symbol, retina_result)

            # Deduplicated staging with stable ids (BUG 3 fix)
            existing_ids = {s.get("id") for s in state.get("active_signals", [])}
            fresh_staged = []

            for sig in lgn_signals:
                sig["symbol"]    = symbol
                sig["staged_at"] = datetime.utcnow().isoformat()
                sig["confirmed"] = False
                sig["status"]    = "pending"
                # Use trigger_price ID — same format as update_m5_state so dedup works
                sig["id"] = (
                    f"{symbol}_{sig['pattern']}_{sig['direction']}_"
                    f"{sig.get('trigger_price')}"
                )
                if sig["id"] not in existing_ids:
                    state.setdefault("active_signals", []).append(sig)
                    existing_ids.add(sig["id"])
                    fresh_staged.append(sig)

            # Collect for Telegram H1 summary
            symbol_results.append({
                "symbol":  symbol,
                "obs":     obs,
                "fvgs":    fvgs,
                "pois":    pois,
                "tls":     tls,
                "signals": fresh_staged,
            })

        except Exception as e:
            print(f"[H1] {symbol} error: {e}")
            traceback.print_exc()
            notify_error(f"H1 {symbol}", str(e))

    # Chart
    try:
        generate_chart(state)
        print(f"\n[H1] Chart generated → output/chart.html")
    except Exception as e:
        print(f"[H1] Chart generation error: {e}")

    save_state(state)
    print(f"\n[H1] Scan complete — state saved")

    # ── Telegram: H1 complete summary ─────────────────────
    notify_h1_complete(symbol_results)


# =========================================================
# M5 MODE — Confirmation, execution and monitoring
# =========================================================
def run_m5_mode():
    from brain.lgn          import run_lgn
    from brain.v1           import run_v1
    from brain.extrastriate import monitor_cycle, register_trades
    from utils.telegram_notifier import (notify_m5_summary,
                                          notify_signals_expired,
                                          notify_error)

    SIGNAL_MAX_AGE_HOURS = 4

    print(f"\n[M5] Starting execution — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    state = load_state()
    now   = datetime.utcnow()

    # ── Guard: don't run if H1 hasn't populated per_symbol yet ──────
    # Without this, M5 loads an empty state, saves it back, and
    # overwrites whatever H1 last wrote — creating an infinite loop
    # where per_symbol is always wiped before M5 can use it.
    if not state.get("per_symbol"):
        print("[M5] No H1 data in state yet — skipping cycle to protect state")
        return

    # ── Pre-pass: expire stale signals, build pending lookup ──
    executed_ids: set  = set()
    fresh_pending: list = []
    just_expired:  list = []   # collected for Telegram notification

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
                    just_expired.append(sig)
                    print(f"[M5] Expired: {sig.get('symbol')} "
                          f"{sig.get('pattern')} {sig.get('direction')}")
                    continue
            except Exception:
                pass

        fresh_pending.append(sig)

    pending_by_symbol: dict = {}
    for sig in fresh_pending:
        pending_by_symbol.setdefault(sig.get("symbol"), []).append(sig)

    # ── Telegram: notify expired signals (one grouped message) ──
    notify_signals_expired(just_expired)

    # ── Counters for M5 summary ────────────────────────────────
    total_placed          = 0
    total_filtered        = 0
    total_staged_consumed = 0
    all_open_trades: list = []

    for symbol in SYMBOLS:
        try:
            retina_data = get_symbol_retina(state, symbol)
            if not retina_data:
                continue

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

            lgn_signals = run_lgn(retina_result, symbol=symbol)

            # ── Max 1 open trade per symbol ────────────────────────────
            # Without this guard the same signal gets placed every 5 minutes
            # producing 10+ duplicate positions on the same setup.
            open_trades = get_open_trades(state, symbol)
            already_open = [t for t in open_trades if t.get("status") == "active"]

            if already_open:
                # Symbol has an open trade — monitor it but don't place new ones
                remaining, context = monitor_cycle(open_trades, symbol)
                all_open_trades.extend(remaining)
                state = update_m5_state(
                    state, symbol,
                    new_signals  = [],
                    trade_updates= remaining
                )
                continue

            # Merge pending staged signals — use trigger_price ID for dedup
            lgn_ids = {
                f"{symbol}_{s['pattern']}_{s['direction']}_{s.get('trigger_price')}"
                for s in lgn_signals
            }

            staged_to_execute = []
            for ps in pending_by_symbol.get(symbol, []):
                if ps.get("id") in executed_ids:
                    continue
                if ps.get("id") not in lgn_ids:
                    staged_to_execute.append(ps)
                    lgn_ids.add(ps.get("id"))

            combined_signals = lgn_signals + staged_to_execute

            # Limit to 1 signal per symbol per cycle.
            # Multiple signals on the same symbol = multiple trades
            # on the same instrument at once, compounding risk.
            # Take the first signal only — LGN already returns
            # the highest-quality match first after deduplication.
            if len(combined_signals) > 1:
                print(f"[M5] {symbol} — {len(combined_signals)} signals, "
                      f"taking highest quality only")
                combined_signals = combined_signals[:1]

            # V1 places trades
            if combined_signals:
                print(f"[M5] {symbol} — {len(lgn_signals)} fresh + "
                      f"{len(staged_to_execute)} staged signal(s) → V1")
                v1_records = run_v1(combined_signals, retina_result, symbol=symbol)
            else:
                v1_records = []

            # Accumulate M5 summary counters
            total_placed   += sum(1 for r in v1_records if r.get("placed"))
            total_filtered += sum(1 for r in v1_records if r.get("filtered"))

            # Mark ALL staged signals for this symbol as executed if any trade
            # was placed — prevents same setup re-firing next cycle with a
            # slightly different trigger_price that wouldn't match placed_ids
            any_placed = any(r.get("placed") for r in v1_records)
            for ps in staged_to_execute:
                if any_placed and ps.get("status") != "executed":
                    ps["status"]    = "executed"
                    ps["confirmed"] = True
                    executed_ids.add(ps.get("id"))
                    total_staged_consumed += 1
                    print(f"[M5]  ✓ Staged signal consumed: "
                          f"{ps['pattern']} {ps['direction']}")

            register_trades(open_trades, v1_records)
            remaining, context = monitor_cycle(open_trades, symbol)

            all_open_trades.extend(remaining)

            state = update_m5_state(
                state, symbol,
                new_signals  = lgn_signals,
                trade_updates= remaining
            )

        except Exception as e:
            print(f"[M5] {symbol} error: {e}")
            traceback.print_exc()
            notify_error(f"M5 {symbol}", str(e))

    # Persist updated signal statuses
    expired_and_done = [
        s for s in state.get("active_signals", [])
        if s.get("status") in ("executed", "expired")
           and s.get("id") in executed_ids
    ]
    state["active_signals"] = fresh_pending + expired_and_done

    save_state(state)
    print(f"\n[M5] Execution cycle complete — state saved")

    # ── Daily report at 21:00 UTC ──────────────────────────
    # Sent once per day to your personal Telegram. Covers all
    # trades closed that calendar day plus any still open.
    now = datetime.utcnow()
    if now.hour == 21 and now.minute < 10:
        last_report = state.get("last_daily_report")
        today       = now.strftime("%Y-%m-%d")
        if last_report != today:
            try:
                from utils.telegram_notifier import notify_daily_report
                date_str     = now.strftime("%d %b %Y")
                closed_today = [
                    t for t in state.get("closed_trades", [])
                    if (t.get("close_time") or "").startswith(today)
                ]
                open_trades  = state.get("open_trades", [])
                notify_daily_report(closed_today, open_trades, date_str)
                state["last_daily_report"] = today
                save_state(state)
                print(f"[M5] Daily report sent for {today}")
            except Exception as e:
                print(f"[M5] Daily report error: {e}")

    # ── Telegram: M5 cycle summary ─────────────────────────
    notify_m5_summary(
        placed          = total_placed,
        filtered        = total_filtered,
        staged_consumed = total_staged_consumed,
        expired         = len(just_expired),
        open_trades     = all_open_trades,
    )


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
