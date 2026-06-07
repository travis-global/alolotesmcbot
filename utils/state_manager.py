"""
utils/state_manager.py
========================
Reads and writes market_state.json.
This is the shared memory between H1 and M5 runs.

H1 writes: order_blocks, fvgs, bos_choch_events, active_signals
M5 reads those and writes: open_trades, closed_trades
"""

import json
import os
from datetime import datetime

STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "state", "market_state.json"
)


def _default_state() -> dict:
    return {
        "order_blocks":     [],
        "fvgs":             [],
        "breakers":         [],
        "bos_choch_events": [],
        "sweeps":           [],
        "trendlines":       [],
        "double_tops":      [],
        "double_bottoms":   [],
        "pois":             [],
        "pd_arrays":        {},
        "structure":        [],
        "swings":           [],
        "active_signals":   [],
        "open_trades":      [],
        "closed_trades":    [],
        "last_h1_run":      None,
        "last_m5_run":      None,
        "symbols":          [],
        "per_symbol":       {}
    }


def load_state() -> dict:
    """Loads state from JSON file. Returns default if file doesn't exist."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)

    if not os.path.exists(STATE_PATH):
        return _default_state()

    try:
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        # Ensure all keys exist (backwards compat)
        default = _default_state()
        for key in default:
            if key not in state:
                state[key] = default[key]
        return state
    except Exception as e:
        print(f"[State] Load error: {e} — using default state")
        return _default_state()


def save_state(state: dict):
    """Saves state to JSON file."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"[State] Save error: {e}")


def update_h1_state(state: dict, symbol: str, retina_result: dict) -> dict:
    """
    Merges Retina H1 results for a symbol into the shared state.
    Called by the H1 workflow after each symbol's Retina run.
    """
    if "per_symbol" not in state:
        state["per_symbol"] = {}

    # Store full per-symbol Retina output
    state["per_symbol"][symbol] = {
        "order_blocks":     retina_result.get("order_blocks",   []),
        "fvgs":             retina_result.get("fvgs",           []),
        "breakers":         retina_result.get("breakers",       []),
        "bos_events":       retina_result.get("bos_events",     []),
        "choch_events":     retina_result.get("choch_events",   []),
        "sweeps":           retina_result.get("sweeps",         []),
        "trendlines":       retina_result.get("trendlines",     []),
        "double_tops":      retina_result.get("double_tops",    []),
        "double_bottoms":   retina_result.get("double_bottoms", []),
        "pois":             retina_result.get("pois",           []),
        "pd_arrays":        retina_result.get("pd_arrays",      {}),
        "structure":        retina_result.get("structure",      []),
        "swings":           retina_result.get("swings",         []),
        "ohlc":             retina_result.get("exec_data",
                            retina_result.get("data",           []))[-100:],
        "updated_at":       datetime.utcnow().isoformat()
    }

    state["last_h1_run"] = datetime.utcnow().isoformat()

    if symbol not in state.get("symbols", []):
        from datetime import datetime
        sig["staged_at"] = datetime.utcnow().isoformat()
        sig.setdefault("status", "pending")
        sig.setdefault("confirmed", False)
        state.setdefault("symbols", []).append(symbol)

    return state


def update_m5_state(state: dict, symbol: str,
                    new_signals: list, trade_updates: list) -> dict:
    """
    Merges M5 LGN signals and trade updates into shared state.
    Called by the M5 workflow after each symbol's LGN + V1 run.
    """
    # Add new signals to active_signals
    existing_ids = {s.get("id") for s in state.get("active_signals", [])}
    for sig in new_signals:
        sig_id = f"{symbol}_{sig.get('pattern')}_{sig.get('direction')}_{sig.get('trigger_price')}"
        sig["id"]     = sig_id
        sig["symbol"] = symbol
        if sig_id not in existing_ids:
            state.setdefault("active_signals", []).append(sig)

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
    still_open  = []
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


def get_symbol_retina(state: dict, symbol: str) -> dict | None:
    """Returns the stored Retina result for a symbol, or None."""
    return state.get("per_symbol", {}).get(symbol)


def get_open_trades(state: dict, symbol: str = None) -> list:
    """Returns open trades, optionally filtered by symbol."""
    trades = state.get("open_trades", [])
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]
    return trades
