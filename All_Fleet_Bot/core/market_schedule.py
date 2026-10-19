"""
Unified Quant Fleet — Market Session & Weekend/Holiday Resumption Manager
Monitors global market sessions, detects weekend/holiday closures, and handles
automatic re-activation and telemetry when markets resume.
"""

from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Dict, Any
from core.audit_logger import get_logger
from core.telegram_notifier import get_notifier

logger = get_logger()


class MarketScheduleGovernor:
    def __init__(
        self,
        weekend_close_utc_day: int = 4,    # Friday
        weekend_close_utc_hour: int = 21,  # 21:00 UTC Friday
        weekend_open_utc_day: int = 6,     # Sunday
        weekend_open_utc_hour: int = 21,   # 21:00 UTC Sunday
        stale_quote_threshold_hours: float = 4.0
    ):
        self.close_day = weekend_close_utc_day
        self.close_hour = weekend_close_utc_hour
        self.open_day = weekend_open_utc_day
        self.open_hour = weekend_open_utc_hour
        self.stale_threshold_sec = stale_quote_threshold_hours * 3600.0

        self.was_market_open: Optional[bool] = None
        self.last_state_change: Optional[datetime] = None

    def is_forex_calendar_open(self, now_utc: datetime) -> bool:
        """
        Check if current time is within normal Forex/Gold/Indices market hours:
          - Friday 21:00 UTC to Sunday 21:00 UTC: CLOSED
          - Sunday 21:00 UTC to Friday 21:00 UTC: OPEN
        """
        weekday = now_utc.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        hour = now_utc.hour

        # Saturday is strictly closed all day
        if weekday == 5:
            return False

        # Friday after close_hour (21:00 UTC) is closed
        if weekday == self.close_day and hour >= self.close_hour:
            return False

        # Sunday before open_hour (21:00 UTC) is closed
        if weekday == self.open_day and hour < self.open_hour:
            return False

        return True

    def check_market_state(
        self,
        now_utc: datetime,
        mt5_mgr: Optional[Any] = None,
        account_info: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, bool, str]:
        """
        Evaluate market open state and detect transition events (e.g. Post-weekend / Post-holiday resumption).
        Returns: (is_open: bool, just_resumed: bool, status_description: str)
        """
        calendar_open = self.is_forex_calendar_open(now_utc)
        is_holiday = False

        # Check if ticks are actively flowing on major symbols on regular weekdays
        if calendar_open and mt5_mgr is not None:
            try:
                tick = mt5_mgr.get_tick("EURUSD")
                if tick and "time" in tick:
                    tick_time = tick["time"]
                    now_ts = now_utc.timestamp()
                    # If tick is older than threshold on a normal weekday -> Market / Bank Holiday
                    if (now_ts - tick_time) > self.stale_threshold_sec and now_utc.weekday() in (0, 1, 2, 3, 4):
                        is_holiday = True
            except Exception:
                pass

        is_open = calendar_open and not is_holiday
        just_resumed = False

        if self.was_market_open is None:
            # First initialization
            self.was_market_open = is_open
            self.last_state_change = now_utc
            status_desc = "OPEN" if is_open else ("HOLIDAY_STANDBY" if is_holiday else "WEEKEND_STANDBY")
            logger.info(f"🌐 [MARKET GOVERNOR] Initial state: {status_desc} (Calendar Open: {calendar_open}, Holiday: {is_holiday})")
            return is_open, False, status_desc

        if not self.was_market_open and is_open:
            # Transition: Market just reopened after weekend or holiday!
            just_resumed = True
            self.was_market_open = True
            self.last_state_change = now_utc
            status_desc = "RESUMED_OPEN"

            logger.info("🌅 [MARKET RESUME] Phục hồi sau phiên nghỉ cuối tuần / ngày lễ! Thị trường đã mở cửa trở lại.")

            # Send Telemetry Announcement
            acct_login = account_info.get("login", "Unknown") if account_info else "Unknown"
            acct_equity = f"${account_info.get('equity', 0.0):,.2f}" if account_info else "N/A"
            msg = (
                "🌅 *MARKET OPENED — QUANT FLEET AUTO-RESUMED*\n"
                "---------------------------------------\n"
                "• *Trạng thái:* Hệ thống tự động kích hoạt sau kỳ nghỉ!\n"
                f"• *Thời gian UTC:* `{now_utc.strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"• *Tài khoản:* `{acct_login}` | *Equity:* `{acct_equity}`\n"
                "• *Hạm đội:* Toàn bộ 5 bot sẵn sàng cho chu kỳ giao dịch tuần mới.\n"
                "---------------------------------------"
            )
            get_notifier().send_message(msg)

        elif self.was_market_open and not is_open:
            # Transition: Market just closed for weekend or holiday
            self.was_market_open = False
            self.last_state_change = now_utc
            status_desc = "HOLIDAY_STANDBY" if is_holiday else "WEEKEND_STANDBY"
            logger.info(f"🌙 [MARKET CLOSED] Thị trường tạm nghỉ ({status_desc}). Chuyển sang chế độ tiết kiệm năng lượng standby.")
        else:
            status_desc = "OPEN" if is_open else ("HOLIDAY_STANDBY" if is_holiday else "WEEKEND_STANDBY")

        return is_open, just_resumed, status_desc
