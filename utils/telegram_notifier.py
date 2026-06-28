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

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN",      "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID",    "")   # broadcast channel
TELEGRAM_PERSONAL_ID = os.getenv("TELEGRAM_PERSONAL_ID", "") # your personal chat

ENABLED          = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
PERSONAL_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_PERSONAL_ID)

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


# =========================================================
# INTERNAL SEND
# =========================================================
def _send(text: str, parse_mode: str = "HTML",
          chat_id: str = None) -> bool:
    """Posts one message to the given chat. Falls back to broadcast channel."""
    target = chat_id or TELEGRAM_CHAT_ID
    if not (TELEGRAM_TOKEN and target):
        return False
    try:
        payload = json.dumps({
            "chat_id":                  target,
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
# 3a. TRAILING STOP MOVED
# =========================================================
def notify_trailing_stop(trade: dict, old_sl: float,
                          new_sl: float, milestone: str):
    """Sent every time the trailing stop moves to a new level."""
    symbol    = trade.get("symbol", "")
    direction = trade.get("direction", "")
    pattern   = trade.get("pattern", "")
    entry     = trade.get("entry")
    tp        = trade.get("tp")
    rr        = trade.get("rr")
    d_emoji   = _dir_emoji(direction)

    lines = [
        f"🔒 <b>Trailing Stop Moved</b>",
        f"",
        f"{d_emoji} <b>{symbol}</b>  {direction.upper()}  ·  {pattern}",
        f"Milestone  :  {milestone}",
        f"SL moved   :  <code>{old_sl}</code>  →  <code>{new_sl}</code>",
        f"Entry      :  <code>{entry}</code>",
        f"TP         :  <code>{tp}</code>",
        f"R:R        :  {rr}",
        f"⏱ {_ts()}",
    ]
    _send("\n".join(lines))


# =========================================================
# 3b. TRADE REJECTED (Deriv API refused the order)
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




# =========================================================
# 8. DAILY TRADE REPORT
# Sent to TELEGRAM_PERSONAL_ID at 23:55 UTC.
# Merges Deriv profit_table (financial truth) with the
# daily_log (bot's reason for entry) for a complete picture.
# =========================================================
def notify_daily_report(date_str: str, log_trades: list = None,
                        open_trades: list = None):
    """
    Fetches today's closed contracts from Deriv, merges with
    the daily_log (written at placement time), and sends a
    detailed report to TELEGRAM_PERSONAL_ID.

    date_str   : "2026-06-21"
    log_trades : list from daily_log.py (pattern, entry, SL, TP, R:R)
    open_trades: list of trades still open in state
    """
    if not PERSONAL_ENABLED:
        print("[Telegram] TELEGRAM_PERSONAL_ID not set — daily report skipped")
        return

    # Fetch from Deriv + merge with daily log
    try:
        from utils.deriv_client import fetch_daily_trades, merge_with_daily_log
        deriv_trades = fetch_daily_trades(date_str)
        trades       = merge_with_daily_log(deriv_trades, log_trades or [])
    except Exception as e:
        print(f"[Telegram] Daily report error: {e}")
        trades = []

    try:
        from datetime import datetime as _dt
        display_date = _dt.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        display_date = date_str

    total    = len(trades)
    wins     = [t for t in trades if t.get("win")]
    losses   = [t for t in trades if not t.get("win")]
    net_pnl  = round(sum(t.get("profit", 0) for t in trades), 2)
    win_rate = round(len(wins) / total * 100) if total else 0
    res_emoji = "✅" if net_pnl > 0 else "❌" if net_pnl < 0 else "⚖️"

    report = [
        f"📊 <b>Daily Report — {display_date}</b>",
        f"",
        f"Trades     :  <b>{total}</b>",
        f"✅ Wins    :  {len(wins)}  ({win_rate}%)",
        f"❌ Losses  :  {len(losses)}  ({100 - win_rate}%)",
        f"Net P&L    :  <b>${net_pnl:+.2f}</b>  {res_emoji}",
    ]

    if trades:
        report.append("")
        report.append("━━━━━━━━━━━━━━━━━━━━━━")

        for i, t in enumerate(trades, 1):
            outcome   = "✅ WIN"  if t.get("win") else "❌ LOSS"
            d_emoji   = "🟢"     if t.get("direction","").upper() == "LONG" else "🔴"
            profit    = t.get("profit",   0)
            pattern   = t.get("pattern",  "—")
            conf      = t.get("confluence","—")
            entry     = t.get("entry")
            sl        = t.get("sl")
            tp        = t.get("tp")
            rr        = t.get("rr")
            mult      = t.get("multiplier")
            symbol    = t.get("display_symbol") or t.get("underlying","—")
            direction = t.get("direction","—")
            buy_time  = t.get("buy_time",  "—")
            sell_time = t.get("sell_time", "—")
            duration  = t.get("duration",  "—")
            stake     = t.get("buy_price", 0)
            payout    = t.get("sell_price",0)
            placed_at = t.get("placed_at", "—")
            longcode  = t.get("longcode",  "")

            report += [
                f"",
                f"<b>Trade {i} — {outcome}</b>",
                f"{d_emoji} <b>{symbol}</b>  {direction}  ·  {pattern}",
            ]

            # Entry details from daily log
            if entry:
                report.append(f"Entry      :  <code>{entry}</code>  "
                               f"(placed {placed_at})")
            if sl and tp:
                report.append(f"SL / TP    :  <code>{sl}</code>  /  "
                               f"<code>{tp}</code>")
            if rr:
                report.append(f"R:R        :  {rr}")
            if conf and conf != "—":
                report.append(f"Reason     :  {conf}")

            # Multiplier
            if mult:
                report.append(f"Multiplier :  x{mult}")

            # Financial outcome from Deriv
            report += [
                f"Open       :  {buy_time} UTC  →  Close: {sell_time} UTC",
                f"Duration   :  {duration}",
                f"Stake      :  ${stake:.2f}  →  Payout: ${payout:.2f}",
                f"Profit     :  <b>${profit:+.2f}</b>",
            ]

            # Longcode (Deriv contract description) if no pattern match
            if pattern == "—" and longcode:
                report.append(f"Contract   :  {longcode[:60]}")

            report.append("━━━━━━━━━━━━━━━━━━━━━━")

    else:
        report += ["", "No closed contracts on Deriv for this day."]

    # Still-open trades
    if open_trades:
        report += ["", f"📂 <b>Still open ({len(open_trades)})</b>"]
        for t in open_trades:
            d = "🟢" if t.get("direction") == "long" else "🔴"
            report.append(
                f"  {d} <b>{t.get('symbol')}</b>  "
                f"{t.get('pattern')} @ <code>{t.get('entry')}</code>  "
                f"R:R {t.get('rr')}"
            )

    report.append(f"\n⏱ Generated {_ts()}")
    _send("\n".join(report), chat_id=TELEGRAM_PERSONAL_ID)
