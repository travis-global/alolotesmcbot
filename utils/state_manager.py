"""
utils/state_manager.py
========================
Split state architecture — eliminates git merge conflicts.

H1 owns: state/h1_state.json  (per_symbol, symbols, last_h1_run)
M5 owns: state/m5_state.json  (active_signals, open_trades, closed_trades,
                                last_m5_run, last_daily_report)

H1 reads  → h1_state.json only
H1 writes → h1_state.json only

M5 reads  → BOTH files (needs per_symbol from H1)
M5 writes → m5_state.json only

Since H1 and M5 never write to the same file, git conflicts
are structurally impossible regardless of timing.
"""

import json
import os
from datetime import datetime

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")

H1_STATE_PATH = os.path.join(_BASE, "h1_state.json")
M5_STATE_PATH = os.path.join(_BASE, "m5_state.json")


# =========================================================
# DEFAULT STATES
# =========================================================
def _default_h1() -> dict:
    return {
        "per_symbol":  {},
        "symbols":     [],
        "last_h1_run": None,
    }


def _default_m5() -> dict:
    return {
        "active_signals":   [],
        "open_trades":      [],
        "closed_trades":    [],
        "last_m5_run":      None,
        "last_daily_report": None,
    }


# =========================================================
# H1 LOAD / SAVE
# =========================================================
def load_h1_state() -> dict:
    os.makedirs(_BASE, exist_ok=True)
    if not os.path.exists(H1_STATE_PATH):
        return _default_h1()
    try:
        with open(H1_STATE_PATH) as f:
            state = json.load(f)
        for k, v in _default_h1().items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        print(f"[State] H1 load error: {e} — using default")
        return _default_h1()


def save_h1_state(state: dict):
    os.makedirs(_BASE, exist_ok=True)
    # Only write H1 keys — never include M5 keys accidentally
    h1_keys = set(_default_h1().keys())
    h1_data = {k: v for k, v in state.items() if k in h1_keys}
    try:
        with open(H1_STATE_PATH, "w") as f:
            json.dump(h1_data, f, indent=2, default=str)
    except Exception as e:
        print(f"[State] H1 save error: {e}")


# =========================================================
# M5 LOAD / SAVE
# =========================================================
def load_m5_state() -> dict:
    os.makedirs(_BASE, exist_ok=True)
    if not os.path.exists(M5_STATE_PATH):
        return _default_m5()
    try:
        with open(M5_STATE_PATH) as f:
            state = json.load(f)
        for k, v in _default_m5().items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        print(f"[State] M5 load error: {e} — using default")
        return _default_m5()


def save_m5_state(state: dict):
    os.makedirs(_BASE, exist_ok=True)
    # Only write M5 keys — never touch H1's file
    m5_keys = set(_default_m5().keys())
    m5_data = {k: v for k, v in state.items() if k in m5_keys}
    try:
        with open(M5_STATE_PATH, "w") as f:
            json.dump(m5_data, f, indent=2, default=str)
    except Exception as e:
        print(f"[State] M5 save error: {e}")


# =========================================================
# FULL STATE (M5 uses this — merges both files)
# =========================================================
def load_full_state() -> dict:
    """
    M5 needs per_symbol from H1 AND active_signals/trades from M5.
    This merges both files into one working dict.
    M5 then calls save_m5_state() — which only writes M5 keys back.
    H1's file is never touched by M5.
    """
    h1 = load_h1_state()
    m5 = load_m5_state()
    return {**h1, **m5}   # M5 keys override on collision (they're separate anyway)


# =========================================================
# UPDATE HELPERS
# =========================================================
def update_h1_state(state: dict, symbol: str, retina_result: dict) -> dict:
    """
    Merges Retina H1 results for a symbol into state.
    Called by H1 workflow after each symbol's Retina run.
    """
    if "per_symbol" not in state:
        state["per_symbol"] = {}

    state["per_symbol"][symbol] = {
        "order_blocks":  retina_result.get("order_blocks",  []),
        "fvgs":          retina_result.get("fvgs",          []),
        "breakers":      retina_result.get("breakers",      []),
        "bos_events":    retina_result.get("bos_events",    []),
        "choch_events":  retina_result.get("choch_events",  []),
        "sweeps":        retina_result.get("sweeps",        []),
        "trendlines":    retina_result.get("trendlines",    []),
        "double_tops":   retina_result.get("double_tops",   []),
        "double_bottoms":retina_result.get("double_bottoms",[]),
        "pois":          retina_result.get("pois",          []),
        "pd_arrays":     retina_result.get("pd_arrays",     {}),
        "structure":     retina_result.get("structure",     []),
        "swings":        retina_result.get("swings",        []),
        "ohlc":          retina_result.get("exec_data",
                         retina_result.get("data",          []))[-100:],
        "updated_at":    datetime.utcnow().isoformat(),
    }

    state["last_h1_run"] = datetime.utcnow().isoformat()

    if symbol not in state.get("symbols", []):
        state.setdefault("symbols", []).append(symbol)

    return state


def update_m5_state(state: dict, symbol: str,
                    new_signals: list, trade_updates: list) -> dict:
    """
    Merges M5 LGN signals and trade updates into state.
    Called by M5 workflow after each symbol's LGN + V1 run.
    """
    # Stage new signals (with dedup)
    existing_ids = {s.get("id") for s in state.get("active_signals", [])}
    for sig in new_signals:
        sig_id = (
            f"{symbol}_{sig.get('pattern')}_"
            f"{sig.get('direction')}_{sig.get('trigger_price')}"
        )
        sig["id"]     = sig_id
        sig["symbol"] = symbol
        if not sig.get("staged_at"):
            sig["staged_at"] = datetime.utcnow().isoformat()
        sig.setdefault("status",    "pending")
        sig.setdefault("confirmed", False)
        if sig_id not in existing_ids:
            state.setdefault("active_signals", []).append(sig)
            existing_ids.add(sig_id)

    # Update open trades
    for update in trade_updates:
        trade_id = update.get("id")
        found    = False
        for i, t in enumerate(state.get("open_trades", [])):
            if t.get("id") == trade_id:
                state["open_trades"][i] = update
                found = True
                break
        if not found and update.get("placed"):
            state.setdefault("open_trades", []).append(update)

    # Move closed trades out of open_trades
    still_open = []
    for t in state.get("open_trades", []):
        if t.get("status") == "closed":
            state.setdefault("closed_trades", []).append(t)
        else:
            still_open.append(t)
    state["open_trades"] = still_open

    # Cap closed trades log at 200
    if len(state.get("closed_trades", [])) > 200:
        state["closed_trades"] = state["closed_trades"][-200:]

    state["last_m5_run"] = datetime.utcnow().isoformat()
    return state


# =========================================================
# LOOKUP HELPERS
# =========================================================
def get_symbol_retina(state: dict, symbol: str) -> dict | None:
    return state.get("per_symbol", {}).get(symbol)


def get_open_trades(state: dict, symbol: str = None) -> list:
    trades = state.get("open_trades", [])
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]
    return trades
