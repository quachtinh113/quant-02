"""
Unified Quant Fleet — Fleet Risk Guard
Account-wide Tier-1 circuit breaker and risk governance.
Guards equity, max drawdown, margin levels, and USD factor concentration across all 5 bots.
Mentored by pysystemtrade (Robert Carver Risk Overlay & Portfolio Risk).
"""

import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from core.audit_logger import get_logger
from core.carver_framework import CarverFactorGuard

logger = get_logger()


class FleetRiskGuard:
    def __init__(
        self,
        max_daily_drawdown_usd: float = 300.0,
        min_margin_level_percent: float = 200.0,
        max_total_positions: int = 10,
        max_net_usd_exposure: int = 4,
        halt_file: str = "HALT_FLEET"
    ):
        self.max_daily_drawdown_usd = max_daily_drawdown_usd
        self.min_margin_level_percent = min_margin_level_percent
        self.max_total_positions = max_total_positions
        self.max_net_usd_exposure = max_net_usd_exposure
        self.halt_file = Path(halt_file)

        self._daily_peak_equity = 0.0
        self._current_date = ""
        self._is_halted = False
        self._halt_reason = ""

    def update_account_state(self, account_info: Dict[str, Any]) -> None:
        """Track daily peak equity and detect reset on new calendar day (UTC)."""
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        current_equity = account_info.get("equity", 0.0)

        # Reset peak on new day
        if today_str != self._current_date:
            self._current_date = today_str
            self._daily_peak_equity = current_equity
            self._is_halted = False
            self._halt_reason = ""
            logger.info(f"🌅 New trading day {today_str}. Reset daily peak equity to ${self._daily_peak_equity:.2f}")

        # Update high-water mark
        if current_equity > self._daily_peak_equity:
            self._daily_peak_equity = current_equity

    def can_open_position(
        self,
        account_info: Dict[str, Any],
        current_position_count: int,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        new_symbol: Optional[str] = None,
        new_direction: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Check whether opening a new position is permitted across all risk dimensions.
        Returns (allowed: bool, reason: str).
        """
        # 1. Manual Kill-Switch Check
        if self.halt_file.exists():
            return False, f"Emergency halt triggered via '{self.halt_file.name}' file."

        # 2. Existing Halt State Check
        if self._is_halted:
            return False, f"Fleet is HALTED: {self._halt_reason}"

        # 3. Max Total Positions Check
        if current_position_count >= self.max_total_positions:
            return False, f"Max fleet positions reached ({current_position_count}/{self.max_total_positions})"

        # 4. Margin Level Guard
        margin_level = account_info.get("margin_level", 0.0)
        # Note: margin_level is 0.0 when no positions are open, so check only when margin > 0
        if account_info.get("margin", 0.0) > 0 and margin_level < self.min_margin_level_percent:
            return False, f"Margin level critical: {margin_level:.1f}% < {self.min_margin_level_percent}% limit"

        # 5. Daily Drawdown Guard
        current_equity = account_info.get("equity", 0.0)
        drawdown_usd = self._daily_peak_equity - current_equity
        if drawdown_usd > self.max_daily_drawdown_usd:
            self._is_halted = True
            self._halt_reason = f"Daily drawdown ${drawdown_usd:.2f} exceeded limit ${self.max_daily_drawdown_usd:.2f}"
            logger.critical(f"🛑 CIRCUIT BREAKER TRIPPED: {self._halt_reason}")
            return False, self._halt_reason

        # 6. Carver Currency & Factor Exposure Guard (Net USD Concentration Check)
        if open_positions is not None and new_symbol and new_direction:
            allowed, factor_reason, resulting_delta = CarverFactorGuard.check_exposure_limit(
                open_positions=open_positions,
                new_symbol=new_symbol,
                new_direction=new_direction,
                max_net_usd_exposure=self.max_net_usd_exposure
            )
            if not allowed:
                logger.warning(f"⚠️ [FACTOR GUARD] {factor_reason}")
                return False, factor_reason

        return True, "OK"

    def get_status(
        self,
        account_info: Dict[str, Any],
        open_positions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Return human-readable risk status summary including USD Factor delta."""
        current_equity = account_info.get("equity", 0.0)
        drawdown_usd = max(0.0, self._daily_peak_equity - current_equity)
        net_usd = CarverFactorGuard.calculate_portfolio_usd_delta(open_positions or [])

        return {
            "is_halted": self._is_halted,
            "halt_reason": self._halt_reason,
            "daily_peak_equity": self._daily_peak_equity,
            "current_drawdown_usd": drawdown_usd,
            "drawdown_limit_usd": self.max_daily_drawdown_usd,
            "margin_level": account_info.get("margin_level", 0.0),
            "net_usd_exposure": net_usd,
            "max_net_usd_exposure": self.max_net_usd_exposure,
        }

