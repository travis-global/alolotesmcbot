"""
utils/telegram_notifier.py
============================
Sends structured Telegram messages for every notable bot event.
Uses only stdlib (urllib) — no extra pip installs needed.

─────────────────────────────────────────
SETUP (one-time, 2 steps)
─────────────────────────────────────────
1. Open Telegram → search @BotFather → /newbot
   Copy the token it gives you.

2. Send your bot any message, then open:
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   Find the "id" inside "chat" → that is your CHAT_ID.

3. Set two environment variables (GitHub Actions → Settings → Secrets):
     TELEGRAM_TOKEN   = 123456789:ABCdef...
     TELEGRAM_CHAT_ID = 987654321

If the env vars are missing the module is silently disabled —
no errors, bot keeps running normally.
─────────────────────────────────────────
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Master switch — False means nothing is sent, no errors raised
ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


# =========================================================
# INTERNAL SEND
# =========================================================
def _send(text: str, parse_mode: str = "HTML") -> bool:
    """Posts one message to the configured chat. Never raises."""
    if not ENABLED:
        return False
    try:
        payload = json.dumps({
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               parse_mode,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            _BASE_URL,
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as e:
        print(f"[Telegram] Send failed: {e}")
        return False


# =========================================================
# SMALL HELPERS
# =========================================================
def _ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def _dir_emoji(direction: str) -> str:
    return "🟢" if direction == "long" else "🔴"


def _pnl_emoji(pnl: float) -> str:
    if pnl > 0:  return "✅"
    if pnl < 0:  return "❌"
    return "⚖️"


def _exit_emoji(exit_type: str) -> str:
    return {
        "tp":        "🎯",
        "sl":        "🛑",
        "condition": "⚠️",
        "manual":    "🤚",
    }.get(exit_type, "❓")


def _sl_pip_display(entry, sl, symbol) -> str:
    """Returns a human-readable pip/point distance for SL display."""
    if entry is None or sl is None:
        return ""
    diff = abs(entry - sl)
    # JPY pairs and high-value instruments use points instead of 4-decimal pips
    if entry > 20:
        return f"  ({diff:.3f} pts)"
    return f"  ({diff / 0.0001:.1f} pips)"


# =========================================================
# 1. H1 SCAN COMPLETE
# =========================================================
def notify_h1_complete(symbol_results: list):
    """
    Called once at the end of run_h1_mode().

    symbol_results — list of dicts, one per symbol:
        {
          "symbol":  "USDJPY",
          "obs":     5,
          "fvgs":    1,
          "pois":    0,
          "tls":     40,
          "signals": [ <lgn signal dicts> ]
        }
    """
    total_signals  = sum(len(r.get("signals", [])) for r in symbol_results)
    active_symbols = [r for r in symbol_results if r.get("signals")]

    lines = [
        f"🔭 <b>H1 Scan Complete</b> — {_ts()}",
        f"Symbols: {len(symbol_results)}  |  Signals staged: <b>{total_signals}</b>",
    ]

    if active_symbols:
        lines.append("")
        for r in active_symbols:
            for sig in r.get("signals", []):
                d = _dir_emoji(sig.get("direction", ""))
                lines.append(
                    f"  {d} <b>{r['symbol']}</b>  "
                    f"{sig.get('pattern')}  {sig.get('direction','').upper()}"
                )
    else:
        lines.append("No new signals this cycle.")

    _send("\n".join(lines))


# =========================================================
# 2. TRADE PLACED
# =========================================================
def notify_trade_placed(record: dict):
    """Called by V1 immediately after a market order is placed."""
    symbol     = record.get("symbol", "")
    direction  = record.get("direction", "")
    pattern    = record.get("pattern", "")
    entry      = record.get("entry")
    sl         = record.get("sl")
    tp         = record.get("tp")
    rr         = record.get("rr")
    confluence = record.get("confluence", [])
    emoji      = _dir_emoji(direction)
    conf_str   = " | ".join(confluence) if confluence else "—"
    sl_display = _sl_pip_display(entry, sl, symbol)

    lines = [
        f"{emoji} <b>TRADE PLACED</b>",
        f"",
        f"<b>{symbol}</b>  {direction.upper()}  ·  {pattern}",
        f"Entry  :  <code>{entry}</code>",
        f"SL     :  <code>{sl}</code>{sl_display}",
        f"TP     :  <code>{tp}</code>",
        f"R:R    :  <b>{rr}</b>",
        f"",
        f"📋 {conf_str}",
        f"⏱ {_ts()}",
    ]
    _send("\n".join(lines))


# =========================================================
# 3a. TRADE REJECTED (Deriv API refused the order)
# =========================================================
def notify_trade_rejected(record: dict, deriv_error: str = ""):
    """Called by V1 when Deriv returns an error on order placement."""
    symbol    = record.get("symbol", "")
    direction = record.get("direction", "")
    pattern   = record.get("pattern", "")
    entry     = record.get("entry")
    sl        = record.get("sl")
    tp        = record.get("tp")
    rr        = record.get("rr")
    sl_display = _sl_pip_display(entry, sl, symbol)

    lines = [
        f"🚫 <b>Order Rejected by Deriv</b>",
        f"",
        f"<b>{symbol}</b>  {direction.upper()}  ·  {pattern}",
        f"Entry  :  <code>{entry}</code>",
        f"SL     :  <code>{sl}</code>{sl_display}",
        f"TP     :  <code>{tp}</code>",
        f"R:R    :  {rr}",
        f"Error  :  {deriv_error}",
        f"⏱ {_ts()}",
    ]
    _send("\n".join(lines))


# =========================================================
# 3b. TRADE FILTERED (V1 rejected it — bad R:R, SL on wrong side, etc.)
# =========================================================
def notify_trade_filtered(record: dict):
    """Called by V1 for every signal that didn't make it to market order."""
    symbol    = record.get("symbol", "")
    direction = record.get("direction", "")
    pattern   = record.get("pattern", "")
    reason    = record.get("filtered_reason", "Unknown")
    entry     = record.get("entry")
    sl        = record.get("sl")
    tp        = record.get("tp")
    rr        = record.get("rr")
    sl_display = _sl_pip_display(entry, sl, symbol)

    lines = [
        f"⚠️ <b>Signal Filtered</b>",
        f"",
        f"<b>{symbol}</b>  {direction.upper()}  ·  {pattern}",
        f"Entry  :  <code>{entry}</code>",
        f"SL     :  <code>{sl}</code>{sl_display}",
        f"TP     :  <code>{tp}</code>",
        f"R:R    :  {rr if rr else '—'}",
        f"Reason :  {reason}",
        f"⏱ {_ts()}",
    ]
    _send("\n".join(lines))


# =========================================================
# 4. TRADE CLOSED
# =========================================================
def notify_trade_closed(trade: dict):
    """Called by Extrastriate _close_record() when a position is exited."""
    symbol      = trade.get("symbol", "")
    direction   = trade.get("direction", "")
    pattern     = trade.get("pattern", "")
    entry       = trade.get("entry")
    close_price = trade.get("close_price")
    pnl         = trade.get("pnl_pips", 0) or 0
    reason      = trade.get("close_reason", "—")
    exit_type   = trade.get("exit_type", "condition")
    placed_time = trade.get("placed_time", "")
    close_time  = trade.get("close_time",  "")

    # Duration
    duration_str = ""
    if placed_time and close_time:
        try:
            fmt       = "%Y-%m-%d %H:%M:%S"
            placed_dt = datetime.strptime(placed_time, fmt)
            close_dt  = datetime.strptime(close_time,  fmt)
            secs      = int((close_dt - placed_dt).total_seconds())
            h, rem    = divmod(secs, 3600)
            m         = rem // 60
            duration_str = f"{h}h {m}m" if h else f"{m}m"
        except Exception:
            pass

    result_emoji = _pnl_emoji(pnl)
    exit_emoji   = _exit_emoji(exit_type)

    lines = [
        f"{exit_emoji} <b>TRADE CLOSED</b>  {result_emoji}",
        f"",
        f"<b>{symbol}</b>  {direction.upper()}  ·  {pattern}",
        f"Entry → Close  :  <code>{entry}</code> → <code>{close_price}</code>",
        f"PnL            :  <b>{pnl:+.1f} pips</b>",
        f"Exit via       :  {exit_type.upper()}",
        f"Reason         :  {reason}",
    ]
    if duration_str:
        lines.append(f"Duration       :  {duration_str}")
    lines.append(f"⏱ {_ts()}")

    _send("\n".join(lines))


# =========================================================
# 5. SIGNALS EXPIRED
# =========================================================
def notify_signals_expired(expired_signals: list):
    """
    Called once per M5 cycle with all signals that just timed out.
    Only fires if expired_signals is non-empty.
    """
    if not expired_signals:
        return

    lines = [
        f"⏰ <b>Signals Expired</b>  ({len(expired_signals)})",
        f"Setup(s) confirmed on H1 but never reached entry:",
        f"",
    ]
    for sig in expired_signals:
        d = _dir_emoji(sig.get("direction", ""))
        lines.append(
            f"  {d} <b>{sig.get('symbol')}</b>  "
            f"{sig.get('pattern')}  {sig.get('direction','').upper()}"
        )
    lines.append(f"\n⏱ {_ts()}")
    _send("\n".join(lines))


# =========================================================
# 6. M5 CYCLE SUMMARY
# Only sent when there is something to report — not every 5 minutes.
# =========================================================
def notify_m5_summary(
    placed:          int,
    filtered:        int,
    staged_consumed: int,
    expired:         int,
    open_trades:     list,
):
    """
    Sent once at the end of run_m5_mode().
    Suppressed entirely if nothing happened and no trades are open.
    This avoids spamming 288 messages per day on quiet market hours.
    """
    is_noteworthy = placed or staged_consumed or expired or open_trades
    if not is_noteworthy:
        return

    lines = [
        f"🔁 <b>M5 Cycle Summary</b> — {_ts()}",
        f"",
        f"Placed       :  {placed}",
        f"Filtered     :  {filtered}",
        f"Staged used  :  {staged_consumed}",
        f"Expired      :  {expired}",
    ]

    if open_trades:
        lines.append("")
        lines.append(f"📂 <b>Open trades  ({len(open_trades)})</b>")
        for t in open_trades:
            d      = _dir_emoji(t.get("direction", ""))
            entry  = t.get("entry")
            rr     = t.get("rr")
            lines.append(
                f"  {d} <b>{t.get('symbol')}</b>  "
                f"{t.get('pattern')} @ <code>{entry}</code>  R:R {rr}"
            )

    _send("\n".join(lines))


# =========================================================
# 7. ERROR ALERT
# =========================================================
def notify_error(context: str, error: str):
    """Call from any except block for errors that need human attention."""
    lines = [
        f"🚨 <b>Bot Error</b>",
        f"Context  :  {context}",
        f"Error    :  <code>{error[:400]}</code>",
        f"⏱ {_ts()}",
    ]
    _send("\n".join(lines))
