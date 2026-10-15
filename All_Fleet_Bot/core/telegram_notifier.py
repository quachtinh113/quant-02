"""
Unified Quant Fleet — Non-Blocking Telemetry & Telegram Notifier
Provides real-time, asynchronous Telegram alerts for order executions, closures,
daily PnL summaries, and circuit breaker risk warnings without stalling the bot loop.
"""

import os
import sys
import json
import time
import queue
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, Any

from core.audit_logger import get_logger

logger = get_logger()

_instance: Optional["TelegramNotifier"] = None
_lock = threading.Lock()


class TelegramNotifier:
    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        config_path: Optional[str] = None
    ):
        # Auto-load .env if python-dotenv is available
        try:
            from dotenv import load_dotenv
            env_file = Path(__file__).resolve().parent.parent / ".env"
            if env_file.exists():
                load_dotenv(env_file)
        except Exception:
            pass

        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "").strip()

        # Fallback to reading config/settings.json or fleet_config.yaml if not in env
        if not (self.token and self.chat_id):
            candidate_paths = [
                config_path,
                str(Path(__file__).resolve().parent.parent / "config" / "settings.json"),
                str(Path(__file__).resolve().parent.parent / "config" / "fleet_config.yaml"),
            ]
            for cp in candidate_paths:
                if cp and Path(cp).exists():
                    self._load_from_config(cp)
                    if self.token and self.chat_id:
                        break

        self.is_configured = bool(self.token and self.chat_id)
        self._warned_unconfigured = False

        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._running = True

        # Start background worker thread
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="TelegramNotifierWorker")
        self._worker_thread.start()

        if self.is_configured:
            logger.info("📱 [TELEMETRY] Telegram Notifier initialized successfully (Non-blocking queue active).")
        else:
            logger.warning("ℹ️ [TELEMETRY] Telegram credentials not configured. Notifications will be logged only.")

    def _load_from_config(self, config_path: str) -> None:
        p = Path(config_path)
        if not p.exists():
            return

        try:
            if p.suffix.lower() == ".json":
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                tele_cfg = cfg.get("telegram", {})
                self.token = self.token or tele_cfg.get("bot_token", "")
                self.chat_id = self.chat_id or tele_cfg.get("chat_id", "")
            elif p.suffix.lower() in (".yaml", ".yml"):
                try:
                    import yaml
                    with open(p, "r", encoding="utf-8") as f:
                        cfg = yaml.safe_load(f) or {}
                    tele_cfg = cfg.get("telegram", {})
                    self.token = self.token or tele_cfg.get("bot_token", "")
                    self.chat_id = self.chat_id or tele_cfg.get("chat_id", "")
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"⚠️ Could not load telegram config from {config_path}: {e}")

    def _worker_loop(self) -> None:
        """Background worker thread that dispatches queued Telegram messages."""
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                endpoint, payload = item
                if not self.is_configured:
                    continue

                url = f"https://api.telegram.org/bot{self.token}/{endpoint}"
                data_bytes = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data_bytes,
                    headers={"Content-Type": "application/json"}
                )

                with urllib.request.urlopen(req, timeout=5.0) as response:
                    pass  # successfully delivered

            except urllib.error.HTTPError as e:
                logger.error(f"❌ [TELEGRAM HTTP {e.code}] Failed to send notification: {e.reason}")
            except Exception as e:
                logger.error(f"❌ [TELEGRAM ERROR] Network error while sending alert: {e}")
            finally:
                self._queue.task_done()

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Enqueue arbitrary message."""
        if not self.is_configured:
            if not self._warned_unconfigured:
                logger.debug("Telegram alert dropped: No bot_token/chat_id configured.")
                self._warned_unconfigured = True
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        try:
            self._queue.put_nowait(("sendMessage", payload))
            return True
        except queue.Full:
            logger.warning("⚠️ [TELEMETRY] Notification queue full. Dropping message.")
            return False

    def send_order_open(
        self,
        ticket: int,
        symbol: str,
        order_type: str,
        lot: float,
        price: float,
        sl: float = 0.0,
        tp: float = 0.0,
        magic: int = 0,
        note: str = ""
    ) -> bool:
        """Format and enqueue order open notification."""
        lines = [
            "🚀 *ORDER EXECUTED*",
            f"• *Ticket:* `#{ticket}`",
            f"• *Asset:* `{symbol}` ({order_type.upper()} `{lot:.2f}L`)",
            f"• *Entry Price:* `{price}`",
            f"• *SL:* `{sl if sl > 0 else 'None'}` | *TP:* `{tp if tp > 0 else 'None'}`",
            f"• *Magic:* `{magic}`",
        ]
        if note:
            lines.append(f"• *Note:* _{note}_")

        return self.send_message("\n".join(lines))

    def send_order_close(
        self,
        ticket: int,
        symbol: str,
        profit: float,
        pips: float = 0.0,
        hold_time: str = "",
        reason: str = ""
    ) -> bool:
        """Format and enqueue order close notification with PnL color."""
        is_win = profit >= 0
        icon = "🎯 *TAKE PROFIT / PROFIT*" if is_win else "🛑 *STOP LOSS / EXIT*"
        sign = "+" if profit > 0 else ""
        pnl_formatted = f"`${sign}{profit:.2f}`"

        lines = [
            icon,
            f"• *Ticket:* `#{ticket}`",
            f"• *Asset:* `{symbol}`",
            f"• *Realized PnL:* {pnl_formatted}",
        ]
        if reason:
            lines.append(f"• *Reason:* _{reason}_")
        if hold_time:
            lines.append(f"• *Duration:* `{hold_time}`")

        return self.send_message("\n".join(lines))

    def send_daily_summary(
        self,
        balance: float,
        equity: float,
        floating: float,
        closed_trades: int,
        daily_pnl: float,
        win_rate: float
    ) -> bool:
        """Format and enqueue daily PnL flash report."""
        pnl_sign = "+" if daily_pnl > 0 else ""
        lines = [
            "📊 *DAILY QUANT FLEET REPORT*",
            "---------------------------------------",
            f"• *Balance:* `${balance:,.2f}`",
            f"• *Equity:* `${equity:,.2f}`",
            f"• *Floating PnL:* `${floating:+,.2f}`",
            f"• *Daily Closed Trades:* `{closed_trades}`",
            f"• *Win Rate:* `{win_rate:.1f}%`",
            f"• *Net Daily PnL:* *${pnl_sign}{daily_pnl:.2f}*",
            "---------------------------------------",
            f"⏰ _{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_"
        ]
        return self.send_message("\n".join(lines))

    def send_risk_alert(self, alert_type: str, message: str) -> bool:
        """Format and enqueue critical risk alarm."""
        lines = [
            f"⚠️ *RISK GUARD ALERT: {alert_type.upper()}*",
            f"• *Details:* {message}",
            f"⏰ _{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_"
        ]
        return self.send_message("\n".join(lines))

    def shutdown(self) -> None:
        """Drain queue and terminate worker cleanly."""
        self._running = False


def get_notifier() -> TelegramNotifier:
    """Singleton getter for centralized telemetry notifier."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = TelegramNotifier()
    return _instance
