"""
metrics.py — Trading Performance Metrics Engine
=================================================
Tracks all key metrics across every trade the bot makes.
Writes to metrics.json after every update.

Metrics are grouped into:
  1. Trade outcomes        — tp/sl/condition close rates
  2. Profitability         — pnl, profit factor, expectancy
  3. Pattern performance   — per-pattern breakdown
  4. Pair performance      — per-symbol breakdown
  5. Signal quality        — filter rates, confluence scores
  6. Time metrics          — duration, session, daily activity
  7. Risk metrics          — drawdown, streak, consistency

Usage:
  from metrics import record_trade, record_filtered, get_metrics

  # When V1 places a trade
  record_placed(trade_record)

  # When Extrastriate closes a trade
  record_closed(trade_record)

  # When V1 filters a signal
  record_filtered(signal, reason)

  # Get full metrics report
  report = get_metrics()
"""

import json
import os
from datetime import datetime
from threading import Lock

# =========================================================
# CONFIG
# =========================================================
METRICS_FILE = os.path.join(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "metrics.json")
)

_lock = Lock()   # thread-safe file writes


# =========================================================
# DEFAULT METRICS STRUCTURE
# =========================================================
def _default_metrics() -> dict:
    return {
        "meta": {
            "created_at":     datetime.utcnow().isoformat(),
            "last_updated":   datetime.utcnow().isoformat(),
            "version":        "1.0"
        },

        # ── Trade outcomes ────────────────────────────────
        "outcomes": {
            "total_placed":         0,
            "total_filtered":       0,
            "tp_hits":              0,
            "sl_hits":              0,
            "condition_closes":     0,
            "still_active":         0,
            "tp_win_rate":          0.0,
            "sl_loss_rate":         0.0,
            "condition_close_rate": 0.0,
            "overall_win_rate":     0.0,   # tp / (tp + sl + condition)
        },

        # ── Profitability ─────────────────────────────────
        "profitability": {
            "total_pnl_pips":       0.0,
            "total_winning_pips":   0.0,
            "total_losing_pips":    0.0,
            "avg_win_pips":         0.0,
            "avg_loss_pips":        0.0,
            "avg_pnl_per_trade":    0.0,
            "profit_factor":        0.0,   # winning_pips / abs(losing_pips)
            "expectancy":           0.0,   # avg pips per trade
            "max_drawdown_pips":    0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "current_streak":       0,     # + = win streak, - = loss streak
        },

        # ── Pattern performance ───────────────────────────
        "patterns": {
            "OB":            _pattern_template(),
            "FVG":           _pattern_template(),
            "BB":            _pattern_template(),
            "POI":           _pattern_template(),
            "Double Top":    _pattern_template(),
            "Double Bottom": _pattern_template(),
            "Trendline":     _pattern_template(),
        },

        # ── Pair performance ──────────────────────────────
        "pairs": {},

        # ── Signal quality ────────────────────────────────
        "signal_quality": {
            "total_lgn_signals":     0,
            "total_v1_filtered":     0,
            "rr_filter_rate":        0.0,   # filtered / signals
            "avg_rr_placed":         0.0,
            "avg_confluence_score":  0.0,
            "confluence_totals":     {},    # counts per confluence tag
        },

        # ── Time metrics ──────────────────────────────────
        "time_metrics": {
            "avg_trade_duration_minutes": 0.0,
            "total_duration_minutes":     0.0,
            "trades_per_day":             {},   # date → count
            "session_performance": {
                "00-06": _session_template(),   # Asian
                "06-12": _session_template(),   # London
                "12-18": _session_template(),   # New York
                "18-24": _session_template(),   # Late
            }
        },

        # ── Risk metrics ──────────────────────────────────
        "risk": {
            "avg_rr_actual":        0.0,   # actual pnl / risk taken
            "best_trade_pips":      0.0,
            "worst_trade_pips":     0.0,
            "running_pnl_curve":    [],    # list of cumulative pnl after each trade
        },

        # ── Raw trade log (last 500) ──────────────────────
        "trade_log": []
    }


def _pattern_template() -> dict:
    return {
        "placed":           0,
        "tp_hits":          0,
        "sl_hits":          0,
        "condition_closes": 0,
        "win_rate":         0.0,
        "avg_pnl_pips":     0.0,
        "total_pnl_pips":   0.0,
        "best_pips":        0.0,
        "worst_pips":       0.0,
    }


def _session_template() -> dict:
    return {
        "trades":   0,
        "wins":     0,
        "losses":   0,
        "win_rate": 0.0,
        "pnl_pips": 0.0,
    }


def _pair_template() -> dict:
    return {
        "placed":           0,
        "tp_hits":          0,
        "sl_hits":          0,
        "condition_closes": 0,
        "win_rate":         0.0,
        "avg_pnl_pips":     0.0,
        "total_pnl_pips":   0.0,
    }


# =========================================================
# LOAD / SAVE
# =========================================================
def _load() -> dict:
    if os.path.exists(METRICS_FILE):
        try:
            with open(METRICS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return _default_metrics()


def _save(metrics: dict):
    metrics["meta"]["last_updated"] = datetime.utcnow().isoformat()
    with open(METRICS_FILE, "w") as f:
        json.dump(metrics, f, indent=2)


# =========================================================
# HELPERS
# =========================================================
def _safe_rate(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator > 0 else 0.0


def _get_session(dt: datetime) -> str:
    hour = dt.hour
    if 0  <= hour < 6:  return "00-06"
    if 6  <= hour < 12: return "06-12"
    if 12 <= hour < 18: return "12-18"
    return "18-24"


def _recalc_profitability(m: dict):
    """Recalculates all derived profitability fields from raw totals."""
    p   = m["profitability"]
    out = m["outcomes"]

    total_closed = out["tp_hits"] + out["sl_hits"] + out["condition_closes"]
    total_wins   = out["tp_hits"]
    total_losses = out["sl_hits"] + out["condition_closes"]

    # Win rates
    out["tp_win_rate"]          = _safe_rate(out["tp_hits"],          total_closed)
    out["sl_loss_rate"]         = _safe_rate(out["sl_hits"],          total_closed)
    out["condition_close_rate"] = _safe_rate(out["condition_closes"],  total_closed)
    out["overall_win_rate"]     = _safe_rate(total_wins,               total_closed)

    # Avg pips
    p["avg_win_pips"]    = _safe_rate(p["total_winning_pips"], total_wins)   if total_wins   > 0 else 0.0
    p["avg_loss_pips"]   = _safe_rate(p["total_losing_pips"],  total_losses) if total_losses > 0 else 0.0
    p["avg_pnl_per_trade"] = _safe_rate(p["total_pnl_pips"],   total_closed) if total_closed > 0 else 0.0

    # Profit factor
    p["profit_factor"] = _safe_rate(
        p["total_winning_pips"],
        abs(p["total_losing_pips"])
    ) if p["total_losing_pips"] != 0 else (
        999.0 if p["total_winning_pips"] > 0 else 0.0
    )

    # Expectancy
    p["expectancy"] = p["avg_pnl_per_trade"]


def _recalc_pattern(pat: dict):
    total = pat["tp_hits"] + pat["sl_hits"] + pat["condition_closes"]
    wins  = pat["tp_hits"]
    pat["win_rate"]      = _safe_rate(wins,                  total)
    pat["avg_pnl_pips"]  = _safe_rate(pat["total_pnl_pips"], total) if total > 0 else 0.0


def _recalc_pair(pair: dict):
    total = pair["tp_hits"] + pair["sl_hits"] + pair["condition_closes"]
    wins  = pair["tp_hits"]
    pair["win_rate"]     = _safe_rate(wins,                   total)
    pair["avg_pnl_pips"] = _safe_rate(pair["total_pnl_pips"], total) if total > 0 else 0.0


def _recalc_signal_quality(m: dict):
    sq = m["signal_quality"]
    sq["rr_filter_rate"] = _safe_rate(
        sq["total_v1_filtered"], sq["total_lgn_signals"]
    )


def _update_streak(m: dict, won: bool):
    p = m["profitability"]
    if won:
        if p["current_streak"] >= 0:
            p["current_streak"] += 1
        else:
            p["current_streak"] = 1
        p["max_consecutive_wins"] = max(
            p["max_consecutive_wins"], p["current_streak"]
        )
    else:
        if p["current_streak"] <= 0:
            p["current_streak"] -= 1
        else:
            p["current_streak"] = -1
        p["max_consecutive_losses"] = max(
            p["max_consecutive_losses"], abs(p["current_streak"])
        )


def _update_drawdown(m: dict):
    curve = m["risk"]["running_pnl_curve"]
    if len(curve) < 2:
        return
    peak    = max(curve)
    trough  = min(curve[curve.index(peak):]) if peak in curve else min(curve)
    drawdown = peak - trough
    m["profitability"]["max_drawdown_pips"] = round(
        max(m["profitability"]["max_drawdown_pips"], drawdown), 2
    )


# =========================================================
# PUBLIC API
# =========================================================
def record_placed(trade: dict):
    """
    Call when V1 places a trade.
    Records placement metadata — outcome recorded later via record_closed.
    """
    with _lock:
        m = _load()

        m["outcomes"]["total_placed"] += 1

        # Signal quality
        sq = m["signal_quality"]
        sq["total_lgn_signals"] += 1

        rr = trade.get("rr", 0) or 0
        placed_count = m["outcomes"]["total_placed"]
        sq["avg_rr_placed"] = round(
            (sq["avg_rr_placed"] * (placed_count - 1) + rr) / placed_count, 3
        )

        # Confluence score
        conf  = trade.get("confluence", [])
        score = len(conf)
        sq["avg_confluence_score"] = round(
            (sq["avg_confluence_score"] * (placed_count - 1) + score) / placed_count, 3
        )
        for tag in conf:
            sq["confluence_totals"][tag] = sq["confluence_totals"].get(tag, 0) + 1

        # Pair tracking
        symbol = trade.get("symbol", "UNKNOWN")
        if symbol not in m["pairs"]:
            m["pairs"][symbol] = _pair_template()
        m["pairs"][symbol]["placed"] += 1

        # Pattern tracking
        pattern = trade.get("pattern", "UNKNOWN")
        if pattern not in m["patterns"]:
            m["patterns"][pattern] = _pattern_template()
        m["patterns"][pattern]["placed"] += 1

        # Daily count
        today = datetime.utcnow().strftime("%Y-%m-%d")
        m["time_metrics"]["trades_per_day"][today] = \
            m["time_metrics"]["trades_per_day"].get(today, 0) + 1

        # Add to trade log (capped at 500)
        m["trade_log"].append({
            "id":          trade.get("id"),
            "symbol":      symbol,
            "pattern":     pattern,
            "direction":   trade.get("direction"),
            "entry":       trade.get("entry"),
            "sl":          trade.get("sl"),
            "tp":          trade.get("tp"),
            "rr":          rr,
            "placed_time": trade.get("placed_time"),
            "status":      "active",
            "outcome":     None,
            "pnl_pips":    None,
            "close_time":  None,
            "close_reason":None,
        })
        if len(m["trade_log"]) > 500:
            m["trade_log"] = m["trade_log"][-500:]

        _save(m)
        print(f"[Metrics] Placed recorded: {symbol} {pattern} {trade.get('direction')}")


def record_filtered(signal: dict, reason: str):
    """
    Call when V1 filters a signal (R:R too low or sanity check failed).
    """
    with _lock:
        m = _load()

        m["outcomes"]["total_filtered"]    += 1
        m["signal_quality"]["total_lgn_signals"] += 1
        m["signal_quality"]["total_v1_filtered"] += 1

        _recalc_signal_quality(m)
        _save(m)


def record_closed(trade: dict):
    """
    Call when Extrastriate closes a trade (TP, SL, or condition violation).
    This is the main metrics update — calculates all derived fields.
    """
    with _lock:
        m = _load()

        exit_type  = trade.get("exit_type",    "condition")
        pnl_pips   = trade.get("pnl_pips",     0.0) or 0.0
        symbol     = trade.get("symbol",        "UNKNOWN")
        pattern    = trade.get("pattern",       "UNKNOWN")
        close_time = trade.get("close_time")
        placed_time= trade.get("placed_time")

        # ── Outcome counters ───────────────────────────────
        out = m["outcomes"]
        if exit_type == "tp":
            out["tp_hits"] += 1
        elif exit_type == "sl":
            out["sl_hits"] += 1
        else:
            out["condition_closes"] += 1

        won = pnl_pips > 0

        # ── Profitability ──────────────────────────────────
        p = m["profitability"]
        p["total_pnl_pips"] = round(p["total_pnl_pips"] + pnl_pips, 2)

        if pnl_pips > 0:
            p["total_winning_pips"] = round(p["total_winning_pips"] + pnl_pips, 2)
        else:
            p["total_losing_pips"]  = round(p["total_losing_pips"]  + pnl_pips, 2)

        p["best_trade_pips"]  = round(max(p["best_trade_pips"],  pnl_pips), 2)
        p["worst_trade_pips"] = round(min(p["worst_trade_pips"], pnl_pips), 2)

        # Running PnL curve
        prev = m["risk"]["running_pnl_curve"][-1] if m["risk"]["running_pnl_curve"] else 0
        m["risk"]["running_pnl_curve"].append(round(prev + pnl_pips, 2))
        if len(m["risk"]["running_pnl_curve"]) > 500:
            m["risk"]["running_pnl_curve"] = m["risk"]["running_pnl_curve"][-500:]

        _update_streak(m, won)
        _update_drawdown(m)
        _recalc_profitability(m)

        # ── Pattern metrics ────────────────────────────────
        if pattern not in m["patterns"]:
            m["patterns"][pattern] = _pattern_template()
        pat = m["patterns"][pattern]
        if exit_type == "tp":
            pat["tp_hits"] += 1
        elif exit_type == "sl":
            pat["sl_hits"] += 1
        else:
            pat["condition_closes"] += 1
        pat["total_pnl_pips"] = round(pat["total_pnl_pips"] + pnl_pips, 2)
        pat["best_pips"]      = round(max(pat["best_pips"],  pnl_pips), 2)
        pat["worst_pips"]     = round(min(pat["worst_pips"], pnl_pips), 2)
        _recalc_pattern(pat)

        # ── Pair metrics ───────────────────────────────────
        if symbol not in m["pairs"]:
            m["pairs"][symbol] = _pair_template()
        pair = m["pairs"][symbol]
        if exit_type == "tp":
            pair["tp_hits"] += 1
        elif exit_type == "sl":
            pair["sl_hits"] += 1
        else:
            pair["condition_closes"] += 1
        pair["total_pnl_pips"] = round(pair["total_pnl_pips"] + pnl_pips, 2)
        _recalc_pair(pair)

        # ── Time metrics ───────────────────────────────────
        if close_time and placed_time:
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                ct  = datetime.strptime(close_time,  fmt)
                pt  = datetime.strptime(placed_time, fmt)
                dur = (ct - pt).total_seconds() / 60

                tm = m["time_metrics"]
                total_closed = (out["tp_hits"] + out["sl_hits"] +
                                out["condition_closes"])
                tm["total_duration_minutes"] = round(
                    tm["total_duration_minutes"] + dur, 1
                )
                tm["avg_trade_duration_minutes"] = round(
                    tm["total_duration_minutes"] / total_closed, 1
                )

                session = _get_session(ct)
                s = tm["session_performance"][session]
                s["trades"] += 1
                if won:
                    s["wins"] += 1
                else:
                    s["losses"] += 1
                s["win_rate"] = _safe_rate(s["wins"], s["trades"])
                s["pnl_pips"] = round(s["pnl_pips"] + pnl_pips, 2)

            except Exception:
                pass

        # ── Update trade log ───────────────────────────────
        trade_id = trade.get("id")
        for log_entry in m["trade_log"]:
            if log_entry.get("id") == trade_id:
                log_entry["status"]       = "closed"
                log_entry["outcome"]      = exit_type
                log_entry["pnl_pips"]     = round(pnl_pips, 2)
                log_entry["close_time"]   = close_time
                log_entry["close_reason"] = trade.get("close_reason")
                break

        _save(m)
        result = "WIN" if won else "LOSS"
        print(
            f"[Metrics] Closed [{result}] {symbol} {pattern} "
            f"{exit_type.upper()} | {pnl_pips:+.1f} pips"
        )


# =========================================================
# READ METRICS
# =========================================================
def get_metrics() -> dict:
    """Returns the full metrics dict."""
    with _lock:
        return _load()


def get_summary() -> dict:
    """Returns a condensed summary for quick reporting."""
    m   = get_metrics()
    out = m["outcomes"]
    p   = m["profitability"]
    sq  = m["signal_quality"]

    total_closed = out["tp_hits"] + out["sl_hits"] + out["condition_closes"]

    return {
        "total_placed":           out["total_placed"],
        "total_closed":           total_closed,
        "total_filtered":         out["total_filtered"],
        "tp_win_rate":            f"{out['tp_win_rate']*100:.1f}%",
        "sl_loss_rate":           f"{out['sl_loss_rate']*100:.1f}%",
        "condition_close_rate":   f"{out['condition_close_rate']*100:.1f}%",
        "overall_win_rate":       f"{out['overall_win_rate']*100:.1f}%",
        "total_pnl_pips":         p["total_pnl_pips"],
        "profit_factor":          p["profit_factor"],
        "expectancy_pips":        p["expectancy"],
        "avg_win_pips":           p["avg_win_pips"],
        "avg_loss_pips":          p["avg_loss_pips"],
        "max_drawdown_pips":      p["max_drawdown_pips"],
        "best_trade_pips":        p["best_trade_pips"],
        "worst_trade_pips":       p["worst_trade_pips"],
        "current_streak":         p["current_streak"],
        "max_win_streak":         p["max_consecutive_wins"],
        "max_loss_streak":        p["max_consecutive_losses"],
        "rr_filter_rate":         f"{sq['rr_filter_rate']*100:.1f}%",
        "avg_rr_placed":          sq["avg_rr_placed"],
        "avg_confluence_score":   sq["avg_confluence_score"],
        "last_updated":           m["meta"]["last_updated"],
    }


def print_report():
    """Prints a formatted metrics report to terminal."""
    m   = get_metrics()
    s   = get_summary()

    print("\n" + "=" * 60)
    print("  SMC BOT — PERFORMANCE REPORT")
    print("=" * 60)

    print(f"\n{'TRADE OUTCOMES':}")
    print(f"  Total placed       : {s['total_placed']}")
    print(f"  Total closed       : {s['total_closed']}")
    print(f"  Filtered by V1     : {s['total_filtered']}")
    print(f"  TP win rate        : {s['tp_win_rate']}")
    print(f"  SL loss rate       : {s['sl_loss_rate']}")
    print(f"  Condition close    : {s['condition_close_rate']}")
    print(f"  Overall win rate   : {s['overall_win_rate']}")

    print(f"\n{'PROFITABILITY':}")
    print(f"  Total PnL          : {s['total_pnl_pips']:+.1f} pips")
    print(f"  Profit factor      : {s['profit_factor']:.2f}")
    print(f"  Expectancy         : {s['expectancy_pips']:+.2f} pips/trade")
    print(f"  Avg win            : {s['avg_win_pips']:+.1f} pips")
    print(f"  Avg loss           : {s['avg_loss_pips']:+.1f} pips")
    print(f"  Best trade         : {s['best_trade_pips']:+.1f} pips")
    print(f"  Worst trade        : {s['worst_trade_pips']:+.1f} pips")
    print(f"  Max drawdown       : {s['max_drawdown_pips']:.1f} pips")
    print(f"  Current streak     : {s['current_streak']:+d}")

    print(f"\n{'PATTERN BREAKDOWN':}")
    for pat, data in m["patterns"].items():
        if data["placed"] > 0:
            print(f"  {pat:15} "
                  f"placed:{data['placed']:3}  "
                  f"win:{data['win_rate']*100:5.1f}%  "
                  f"pnl:{data['total_pnl_pips']:+.1f} pips")

    print(f"\n{'PAIR BREAKDOWN':}")
    for sym, data in m["pairs"].items():
        if data["placed"] > 0:
            print(f"  {sym:25} "
                  f"placed:{data['placed']:3}  "
                  f"win:{data['win_rate']*100:5.1f}%  "
                  f"pnl:{data['total_pnl_pips']:+.1f} pips")

    print(f"\n{'SESSION BREAKDOWN':}")
    sessions = {"00-06": "Asian", "06-12": "London",
                "12-18": "New York", "18-24": "Late"}
    for key, name in sessions.items():
        s_data = m["time_metrics"]["session_performance"][key]
        if s_data["trades"] > 0:
            print(f"  {name:10} "
                  f"trades:{s_data['trades']:3}  "
                  f"win:{s_data['win_rate']*100:5.1f}%  "
                  f"pnl:{s_data['pnl_pips']:+.1f} pips")

    print(f"\n{'SIGNAL QUALITY':}")
    print(f"  R:R filter rate    : {get_summary()['rr_filter_rate']}")
    print(f"  Avg R:R placed     : {s['avg_rr_placed']:.2f}")
    print(f"  Avg confluence     : {s['avg_confluence_score']:.1f} factors")

    print(f"\n  Last updated: {s['last_updated']}")
    print("=" * 60 + "\n")


# =========================================================
# RESET (for fresh test runs)
# =========================================================
def reset_metrics():
    """Resets all metrics. Use before a fresh test period."""
    with _lock:
        m = _default_metrics()
        _save(m)
        print("[Metrics] Reset complete — fresh metrics file created")


# =========================================================
# ENTRY POINT — print current report
# =========================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        confirm = input("Reset all metrics? This cannot be undone. (yes/no): ")
        if confirm.lower() == "yes":
            reset_metrics()
        else:
            print("Reset cancelled")
    else:
        print_report()
