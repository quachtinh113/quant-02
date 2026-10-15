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
from core.telegram_notifier import get_notifier

logger = get_logger()


class FleetRiskGuard:
    def __init__(
        self,
        max_daily_drawdown_usd: float = 300.0,
        min_margin_level_percent: float = 200.0,
        max_total_positions: int = 10,
        max_net_usd_exposure: int = 4,
        max_directional_usd_exposure: int = 3,
        halt_file: str = "HALT_FLEET"
    ):
        self.max_daily_drawdown_usd = max_daily_drawdown_usd
        self.min_margin_level_percent = min_margin_level_percent
        self.max_total_positions = max_total_positions
        self.max_net_usd_exposure = max_net_usd_exposure
        self.max_directional_usd_exposure = max_directional_usd_exposure
        self.halt_file = Path(halt_file)

        self._daily_peak_equity = 0.0
        self._current_date = ""
        self._is_halted = False
        self._halt_reason = ""

    def update_account_state(self, account_info: Dict[str, Any], current_date_str: Optional[str] = None) -> None:
        """Track daily peak equity and detect reset on new calendar day (UTC)."""
        today_str = current_date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        account_info_or_symbol: Any = None,
        current_position_count: Any = None,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        new_symbol: Optional[str] = None,
        new_direction: Optional[str] = None,
        symbol: Optional[str] = None,
        order_type: Optional[str] = None,
        **kwargs: Any
    ) -> Tuple[bool, str]:
        """
        Check whether opening a new position is permitted across all risk dimensions.
        Supports:
          1. can_open_position(account_info, current_position_count=2, open_positions=..., new_symbol=..., new_direction=...)
          2. can_open_position(symbol, order_type)
        Returns (allowed: bool, reason: str).
        """
        # Resolve polymorphic arguments
        account_info: Dict[str, Any] = {}
        pos_count = 0
        target_symbol: Optional[str] = None
        target_direction: Optional[str] = None

        if isinstance(account_info_or_symbol, str):
            # Called as can_open_position("EURUSD", "BUY")
            target_symbol = account_info_or_symbol
            target_direction = order_type or current_position_count or kwargs.get("direction")
        elif isinstance(account_info_or_symbol, dict):
            account_info = account_info_or_symbol
            if isinstance(current_position_count, int):
                pos_count = current_position_count
            elif isinstance(kwargs.get("current_position_count"), int):
                pos_count = kwargs["current_position_count"]
            target_symbol = new_symbol or symbol or kwargs.get("symbol")
            target_direction = new_direction or order_type or kwargs.get("direction")
        else:
            target_symbol = symbol or new_symbol or kwargs.get("symbol")
            target_direction = order_type or new_direction or kwargs.get("direction")

        # Fallback to query MT5 positions if open_positions was not provided
        if open_positions is None:
            try:
                import MetaTrader5 as mt5
                raw_pos = mt5.positions_get()
                if raw_pos is not None:
                    open_positions = [
                        {"symbol": p.symbol, "type": "BUY" if p.type == 0 else "SELL"}
                        for p in raw_pos
                    ]
            except Exception:
                open_positions = []

        if pos_count == 0 and open_positions is not None:
            pos_count = len(open_positions)

        # 1. Manual Kill-Switch Check
        if self.halt_file.exists():
            reason = f"Emergency halt triggered via '{self.halt_file.name}' file."
            get_notifier().send_risk_alert("EMERGENCY_HALT", reason)
            return False, reason

        # 2. Existing Halt State Check
        if self._is_halted:
            return False, f"Fleet is HALTED: {self._halt_reason}"

        # 3. Max Total Positions Check
        if pos_count >= self.max_total_positions:
            return False, f"Max fleet positions reached ({pos_count}/{self.max_total_positions})"

        # 4. Margin Level Guard
        if account_info:
            margin_level = account_info.get("margin_level", 0.0)
            if account_info.get("margin", 0.0) > 0 and margin_level < self.min_margin_level_percent:
                reason = f"Margin level critical: {margin_level:.1f}% < {self.min_margin_level_percent}% limit"
                return False, reason

        # 5. Daily Drawdown Guard
        if account_info and self._daily_peak_equity > 0:
            current_equity = account_info.get("equity", 0.0)
            drawdown_usd = self._daily_peak_equity - current_equity
            if drawdown_usd > self.max_daily_drawdown_usd:
                self._is_halted = True
                self._halt_reason = f"Daily drawdown ${drawdown_usd:.2f} exceeded limit ${self.max_daily_drawdown_usd:.2f}"
                logger.critical(f"🛑 CIRCUIT BREAKER TRIPPED: {self._halt_reason}")
                get_notifier().send_risk_alert("DRAWDOWN_BREACH", self._halt_reason)
                return False, self._halt_reason

        # 6. Directional USD Factor Concentration Guard (Max Ceiling: 3)
        if open_positions is not None and target_symbol and target_direction:
            allowed, dir_reason = CarverFactorGuard.check_directional_exposure_limit(
                open_positions=open_positions,
                new_symbol=target_symbol,
                new_direction=target_direction,
                max_directional_usd=self.max_directional_usd_exposure
            )
            if not allowed:
                logger.warning(f"⚠️ [FACTOR GUARD] {dir_reason}")
                get_notifier().send_risk_alert("USD_FACTOR_VETO", f"{target_direction} {target_symbol}: {dir_reason}")
                return False, dir_reason

            # 7. Carver Net USD Portfolio Exposure Guard
            allowed, factor_reason, resulting_delta = CarverFactorGuard.check_exposure_limit(
                open_positions=open_positions,
                new_symbol=target_symbol,
                new_direction=target_direction,
                max_net_usd_exposure=self.max_net_usd_exposure
            )
            if not allowed:
                logger.warning(f"⚠️ [FACTOR GUARD] {factor_reason}")
                get_notifier().send_risk_alert("USD_NET_FACTOR_VETO", f"{target_direction} {target_symbol}: {factor_reason}")
                return False, factor_reason

        return True, "OK"

    def get_status(
        self,
        account_info: Dict[str, Any],
        open_positions: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """Return human-readable risk status summary including USD Factor metrics."""
        current_equity = account_info.get("equity", 0.0)
        drawdown_usd = max(0.0, self._daily_peak_equity - current_equity)
        positions = open_positions or []
        net_usd = CarverFactorGuard.calculate_portfolio_usd_delta(positions)
        long_usd, short_usd = CarverFactorGuard.count_directional_usd_positions(positions)

        return {
            "is_halted": self._is_halted,
            "halt_reason": self._halt_reason,
            "daily_peak_equity": self._daily_peak_equity,
            "current_drawdown_usd": drawdown_usd,
            "drawdown_limit_usd": self.max_daily_drawdown_usd,
            "margin_level": account_info.get("margin_level", 0.0),
            "net_usd_exposure": net_usd,
            "max_net_usd_exposure": self.max_net_usd_exposure,
            "long_usd_count": long_usd,
            "short_usd_count": short_usd,
            "max_directional_usd_exposure": self.max_directional_usd_exposure,
        }
