"""
utils/daily_log.py
===================
Writes every placed trade to a daily JSON log file.
One file per day: state/daily_log_YYYY-MM-DD.json

At 23:55 UTC the daily report reads this file, merges it with
Deriv's profit_table (actual financial outcome), sends the
report to your personal Telegram, then clears the file.

Why a separate file and not state?
  - h1_state.json and m5_state.json have git conflict issues
  - closed_trades in state proved unreliable
  - This file is write-once (trade placed) and read-once
    (report time) — no concurrent writes, no conflicts
"""

import json
import os
from datetime import datetime, timezone

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")


def _log_path(date_str: str) -> str:
    """Returns full path to the daily log for a given date (YYYY-MM-DD)."""
    return os.path.join(_BASE, f"daily_log_{date_str}.json")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log_placed_trade(record: dict):
    """
    Appends a placed trade to today's daily log.
    Called by V1 immediately after Deriv confirms the order.

    Stores everything the report will need from the bot's side:
    contract_id, symbol, direction, pattern, entry, sl, tp,
    rr, stake, multiplier, confluence, placed_at.
    """
    date_str = _today()
    path     = _log_path(date_str)
    os.makedirs(_BASE, exist_ok=True)

    # Load existing log
    try:
        with open(path) as f:
            log = json.load(f)
    except Exception:
        log = {"date": date_str, "trades": []}

    # Extract contract_id from mt5_result
    mt5 = record.get("mt5_result") or {}
    contract_id = mt5.get("contract_id")

    entry = {
        "contract_id": contract_id,
        "symbol":      record.get("symbol"),
        "direction":   record.get("direction"),
        "pattern":     record.get("pattern"),
        "entry":       record.get("entry"),
        "sl":          record.get("sl"),
        "tp":          record.get("tp"),
        "rr":          record.get("rr"),
        "stake":       mt5.get("buy_price") or record.get("stake"),
        "multiplier":  record.get("multiplier"),
        "confluence":  record.get("confluence", []),
        "zone_top":    record.get("zone_top"),
        "zone_bottom": record.get("zone_bottom"),
        "placed_at":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    # Avoid duplicates (same contract_id placed twice somehow)
    existing_ids = {t.get("contract_id") for t in log["trades"] if t.get("contract_id")}
    if contract_id and contract_id in existing_ids:
        print(f"[DailyLog] Contract {contract_id} already in log — skipping")
        return

    log["trades"].append(entry)

    with open(path, "w") as f:
        json.dump(log, f, indent=2)

    print(f"[DailyLog] Logged: {record.get('symbol')} "
          f"{record.get('direction')} {record.get('pattern')} "
          f"(contract {contract_id})")


def load_daily_log(date_str: str = None) -> dict:
    """Loads the daily log for a given date. Defaults to today."""
    date_str = date_str or _today()
    path     = _log_path(date_str)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"date": date_str, "trades": []}


def clear_daily_log(date_str: str = None):
    """
    Resets the daily log after the report has been sent.
    Keeps the file but empties the trades list so it's
    ready for the next day without needing deletion.
    """
    date_str = date_str or _today()
    path     = _log_path(date_str)
    try:
        with open(path, "w") as f:
            json.dump({"date": date_str, "trades": [], "reported": True}, f, indent=2)
        print(f"[DailyLog] Cleared log for {date_str}")
    except Exception as e:
        print(f"[DailyLog] Clear error: {e}")
