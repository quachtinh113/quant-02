"""
Unified Quant Fleet — Fleet Risk Guard
Account-wide Tier-1 circuit breaker and risk governance.
Guards equity, max drawdown, margin levels, USD factor concentration,
and enforces Daily PnL Governor (Profit Target Lockout & Daily Loss Breaker).
Mentored by pysystemtrade (Robert Carver Risk Overlay & Portfolio Risk).
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from core.audit_logger import get_logger
from core.carver_framework import CarverFactorGuard
from core.telegram_notifier import get_notifier

logger = get_logger()


class CrossAssetCorrelationGuard:
    """
    Monitors cross-asset correlation across the 5 bot strategies:
      - FX Pairs (EURUSD, GBPUSD, USDCAD, USDJPY, AUDUSD)
      - Precious Metals (XAUUSD)
      - US Equity Indices (US500, USTEC)
      - Crypto (BTCUSD)
    Calculates rolling Pearson correlation matrix and scales down position sizing
    by 30% - 50% if adding a new trade exceeds cross-correlation threshold (|rho| > 0.75).
    """
    BASELINE_CORRELATION: Dict[Tuple[str, str], float] = {
        ("USTEC", "US500"): 0.94,
        ("US500", "USTEC"): 0.94,
        ("EURUSD", "GBPUSD"): 0.80,
        ("GBPUSD", "EURUSD"): 0.80,
        ("AUDUSD", "USDCAD"): -0.74,
        ("USDCAD", "AUDUSD"): -0.74,
        ("AUDUSD", "EURUSD"): 0.76,
        ("EURUSD", "AUDUSD"): 0.76,
        ("AUDUSD", "GBPUSD"): 0.72,
        ("GBPUSD", "AUDUSD"): 0.72,
        ("XAUUSD", "US500"): -0.38,
        ("US500", "XAUUSD"): -0.38,
        ("XAUUSD", "USTEC"): -0.35,
        ("USTEC", "XAUUSD"): -0.35,
        ("XAUUSD", "EURUSD"): 0.45,
        ("EURUSD", "XAUUSD"): 0.45,
        ("BTCUSD", "USTEC"): 0.55,
        ("USTEC", "BTCUSD"): 0.55,
    }

    def __init__(self, correlation_threshold: float = 0.75, scale_discount: float = 0.60):
        self.threshold = correlation_threshold
        self.scale_discount = scale_discount  # 40% reduction (0.60x size)
        self.dynamic_matrix: Dict[Tuple[str, str], float] = dict(self.BASELINE_CORRELATION)
        self.last_update_time: Optional[datetime] = None

    def update_rolling_matrix(self, mt5_mgr: Any, days: int = 30) -> bool:
        """Fetch D1 closes from MT5 and recompute rolling Pearson correlation matrix."""
        try:
            import pandas as pd
            import math
            symbols = ["EURUSD", "GBPUSD", "USDCAD", "USDJPY", "AUDUSD", "XAUUSD", "US500", "USTEC", "BTCUSD"]
            series_dict = {}

            for s in symbols:
                df = mt5_mgr.get_rates(s, "D1", count=days + 5)
                if df is not None and len(df) >= days:
                    series_dict[s] = df['close'].pct_change().dropna()

            if len(series_dict) >= 3:
                df_returns = pd.DataFrame(series_dict).dropna()
                corr_df = df_returns.corr(method="pearson")
                for col1 in corr_df.columns:
                    for col2 in corr_df.columns:
                        if col1 != col2:
                            val = float(corr_df.loc[col1, col2])
                            if not math.isnan(val):
                                self.dynamic_matrix[(col1, col2)] = round(val, 3)

                self.last_update_time = datetime.now(timezone.utc)
                logger.info(f"📊 [CORRELATION GUARD] Dynamic 30-day Rolling Pearson Matrix updated ({len(self.dynamic_matrix)} pairs).")
                return True
        except Exception as e:
            logger.warning(f"⚠️ [CORRELATION GUARD] Could not recompute dynamic matrix: {e}")
        return False

    def get_correlation(self, sym1: str, sym2: str) -> float:
        c1 = sym1.rstrip("m").upper()
        c2 = sym2.rstrip("m").upper()
        if c1 == c2:
            return 1.0
        return self.dynamic_matrix.get((c1, c2), self.dynamic_matrix.get((c2, c1), 0.0))

    def evaluate_new_position_correlation(
        self,
        open_positions: List[Dict[str, Any]],
        new_symbol: str,
        new_direction: str
    ) -> Tuple[float, str]:
        """
        Check whether opening new_symbol adds severe concentrated factor risk
        given existing open positions:
          - If aligned directional correlation >= threshold (0.75):
            Returns (scale_discount, reason) e.g. 0.60x size
          - Otherwise returns (1.0, "Normal correlation")
        """
        if not open_positions:
            return 1.0, "No open positions"

        new_sym_clean = new_symbol.rstrip("m").upper()
        new_dir = str(new_direction).upper()

        max_detected_corr = 0.0
        conflict_info = ""

        for pos in open_positions:
            pos_sym = str(pos.get("symbol", "")).rstrip("m").upper()
            pos_dir = str(pos.get("type", "BUY")).upper()
            if pos_sym == new_sym_clean:
                continue

            raw_corr = self.get_correlation(new_sym_clean, pos_sym)

            # Determine aligned directional exposure
            if new_dir == pos_dir:
                aligned_risk_corr = raw_corr
            else:
                aligned_risk_corr = -raw_corr

            if aligned_risk_corr > max_detected_corr:
                max_detected_corr = aligned_risk_corr
                conflict_info = f"{pos_sym} {pos_dir} (#{pos.get('ticket')}, rho={raw_corr:+.2f})"

        if max_detected_corr >= self.threshold:
            scale = self.scale_discount
            reason = (
                f"High cross-asset correlation ({max_detected_corr:.2f} >= {self.threshold:.2f}) "
                f"with open {conflict_info}. Scaling lot size down by {int((1-scale)*100)}% ({scale:.2f}x)."
            )
            return scale, reason

        return 1.0, "Cross-asset correlation within safety tolerance"


class FleetRiskGuard:
    def __init__(
        self,
        max_daily_drawdown_usd: float = 300.0,
        min_margin_level_percent: float = 200.0,
        max_total_positions: int = 10,
        max_net_usd_exposure: int = 4,
        max_directional_usd_exposure: int = 3,
        daily_profit_target_min: float = 50.0,
        daily_profit_target_max: float = 100.0,
        daily_max_loss_limit: float = 100.0,
        reset_hour_utc: int = 0,
        risk_limits_file: Optional[str] = None,
        halt_file: str = "HALT_FLEET"
    ):
        self.max_daily_drawdown_usd = max_daily_drawdown_usd
        self.min_margin_level_percent = min_margin_level_percent
        self.max_total_positions = max_total_positions
        self.max_net_usd_exposure = max_net_usd_exposure
        self.max_directional_usd_exposure = max_directional_usd_exposure
        self.halt_file = Path(halt_file)

        # Cross-Asset Correlation Guard
        self.correlation_guard = CrossAssetCorrelationGuard()

        # Daily PnL Governor thresholds
        self.daily_profit_target_min = daily_profit_target_min
        self.daily_profit_target_max = daily_profit_target_max
        self.daily_max_loss_limit = daily_max_loss_limit
        self.reset_hour_utc = reset_hour_utc

        # Load limits override from risk_limits.json if present
        self._load_risk_limits(risk_limits_file)

        self._daily_peak_equity = 0.0
        self._current_date = ""
        self._is_halted = False
        self._halt_reason = ""

        # Daily PnL Governor state
        self._today_realized_pnl = 0.0
        self._today_deals_count = 0
        self._profit_locked = False
        self._daily_loss_tripped = False

    def _load_risk_limits(self, custom_path: Optional[str] = None) -> None:
        """Load external risk limits configuration from JSON if available."""
        p = Path(custom_path) if custom_path else Path(__file__).resolve().parent.parent / "config" / "risk_limits.json"
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                if "daily_profit_target_min" in cfg:
                    self.daily_profit_target_min = float(cfg["daily_profit_target_min"])
                if "daily_profit_target_max" in cfg:
                    self.daily_profit_target_max = float(cfg["daily_profit_target_max"])
                if "daily_max_loss_limit" in cfg:
                    self.daily_max_loss_limit = float(cfg["daily_max_loss_limit"])
                if "reset_hour_utc" in cfg:
                    self.reset_hour_utc = int(cfg["reset_hour_utc"])
                logger.info(
                    f"⚙️ [RISK GOVERNOR] Loaded limits from {p.name}: "
                    f"Target1=${self.daily_profit_target_min}, Target2=${self.daily_profit_target_max}, "
                    f"MaxLoss=-${self.daily_max_loss_limit}, Reset={self.reset_hour_utc:02d}:00 UTC"
                )
            except Exception as e:
                logger.warning(f"⚠️ Could not load risk limits from {p}: {e}")

    @property
    def is_profit_locked(self) -> bool:
        """Check if Daily Profit Target 2 (+100$) has locked out new entries."""
        return self._profit_locked

    @property
    def is_daily_loss_tripped(self) -> bool:
        """Check if Daily Loss Circuit Breaker (-100$) has been triggered."""
        return self._daily_loss_tripped

    def get_risk_scale(self) -> float:
        """
        Dynamic risk multiplier for new trade sizing:
          - 0.0 if Target 2 locked or daily loss breaker tripped
          - 0.5 if Target 1 (+50$) reached (protecting profits)
          - 1.0 normal baseline
        """
        if self._profit_locked or self._daily_loss_tripped:
            return 0.0
        if self._today_realized_pnl >= self.daily_profit_target_max:
            return 0.0
        if self._today_realized_pnl >= self.daily_profit_target_min:
            return 0.5
        return 1.0

    def get_correlation_scale(
        self,
        open_positions: Optional[List[Dict[str, Any]]],
        new_symbol: str,
        new_direction: str
    ) -> float:
        """Return lot sizing scale multiplier (0.60x to 1.0x) based on cross-asset correlation."""
        if not open_positions or not new_symbol or not new_direction:
            return 1.0
        scale, reason = self.correlation_guard.evaluate_new_position_correlation(
            open_positions=open_positions,
            new_symbol=new_symbol,
            new_direction=new_direction
        )
        if scale < 1.0:
            logger.info(f"🛡️ [RISK GUARD CORRELATION] {reason}")
            try:
                get_notifier().send_risk_alert("CORRELATION_SCALE", f"{new_direction} {new_symbol}: {reason}")
            except Exception:
                pass
        return scale

    def get_today_realized_pnl(self, mt5_mgr: Optional[Any] = None) -> float:
        """
        Query MT5 closed deals from 00:00:00 UTC today to current time:
          Daily PnL = Sum(Deal Profit + Swap + Commission)
        """
        now_utc = datetime.now(timezone.utc)
        start_today = datetime(now_utc.year, now_utc.month, now_utc.day, self.reset_hour_utc, 0, 0, tzinfo=timezone.utc)

        total_pnl = 0.0
        deal_count = 0

        try:
            if mt5_mgr is not None and hasattr(mt5_mgr, "get_history_deals"):
                deals = mt5_mgr.get_history_deals(start_today, now_utc)
                for d in deals:
                    total_pnl += float(d.get("net_profit", d.get("profit", 0.0) + d.get("swap", 0.0) + d.get("commission", 0.0)))
                deal_count = len(deals)
            else:
                import MetaTrader5 as mt5
                raw_deals = mt5.history_deals_get(start_today, now_utc)
                if raw_deals:
                    for d in raw_deals:
                        if getattr(d, 'type', 0) == 2:  # skip DEAL_TYPE_BALANCE
                            continue
                        total_pnl += float(d.profit + d.swap + d.commission)
                        deal_count += 1

            self._today_realized_pnl = round(total_pnl, 2)
            self._today_deals_count = deal_count

            # Check Target 2 Lockout (+100$)
            if self._today_realized_pnl >= self.daily_profit_target_max:
                if not self._profit_locked:
                    self._profit_locked = True
                    msg = (
                        f"🎯 Daily Profit Target 2 reached (${self._today_realized_pnl:+.2f} >= ${self.daily_profit_target_max:.2f}). "
                        f"Hệ thống kích hoạt chế độ LOCK LÃI, dừng mở lệnh mới để bảo toàn lợi nhuận."
                    )
                    logger.warning(f"🎯 [PROFIT LOCK] {msg}")
                    get_notifier().send_risk_alert("PROFIT_LOCKOUT", msg)

            # Check Daily Max Loss Breaker (-100$)
            if self._today_realized_pnl <= -self.daily_max_loss_limit:
                if not self._daily_loss_tripped:
                    self._daily_loss_tripped = True
                    msg = (
                        f"🛑 Daily Max Loss limit reached (${self._today_realized_pnl:+.2f} <= -${self.daily_max_loss_limit:.2f}). "
                        f"Kích hoạt ngắt mạch cắt lỗ ngày (Daily Circuit Breaker), từ chối mở lệnh mới."
                    )
                    logger.critical(f"🛑 [DAILY LOSS BREAKER] {msg}")
                    get_notifier().send_risk_alert("DAILY_LOSS_BREACH", msg)

        except Exception as e:
            logger.warning(f"⚠️ Error querying today realized PnL: {e}")

        return self._today_realized_pnl

    def update_account_state(
        self,
        account_info: Dict[str, Any],
        current_date_str: Optional[str] = None,
        mt5_mgr: Optional[Any] = None
    ) -> None:
        """Track daily peak equity, update today PnL, and reset governor on new calendar day (00:00 UTC)."""
        today_str = current_date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current_equity = account_info.get("equity", 0.0)

        # Reset peak and Daily PnL Governor on new day
        if today_str != self._current_date:
            self._current_date = today_str
            self._daily_peak_equity = current_equity
            self._is_halted = False
            self._halt_reason = ""
            self._profit_locked = False
            self._daily_loss_tripped = False
            self._today_realized_pnl = 0.0
            self._today_deals_count = 0
            logger.info(f"🌅 New trading day {today_str}. Reset Daily PnL Governor and equity watermark to ${self._daily_peak_equity:.2f}")

        # Update high-water mark
        if current_equity > self._daily_peak_equity:
            self._daily_peak_equity = current_equity

        # Refresh today's realized PnL
        if mt5_mgr is not None:
            self.get_today_realized_pnl(mt5_mgr)

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

        # 3. Daily Profit Target 2 Lockout Check (+100$)
        if self._profit_locked or self._today_realized_pnl >= self.daily_profit_target_max:
            self._profit_locked = True
            reason = (
                f"Daily Profit Target Reached (${self._today_realized_pnl:+.2f} >= ${self.daily_profit_target_max:.2f}). "
                f"Hệ thống kích hoạt chế độ LOCK LÃI, dừng mở lệnh mới để bảo toàn lợi nhuận."
            )
            return False, reason

        # 4. Daily Max Loss Circuit Breaker Check (-100$)
        if self._daily_loss_tripped or self._today_realized_pnl <= -self.daily_max_loss_limit:
            self._daily_loss_tripped = True
            reason = (
                f"Daily Max Loss Limit Breached (${self._today_realized_pnl:+.2f} <= -${self.daily_max_loss_limit:.2f}). "
                f"Ngắt mạch cắt lỗ ngày kích hoạt, từ chối mở lệnh mới."
            )
            return False, reason

        # 5. Max Total Positions Check
        if pos_count >= self.max_total_positions:
            return False, f"Max fleet positions reached ({pos_count}/{self.max_total_positions})"

        # 6. Margin Level Guard
        if account_info:
            margin_level = account_info.get("margin_level", 0.0)
            if account_info.get("margin", 0.0) > 0 and margin_level < self.min_margin_level_percent:
                reason = f"Margin level critical: {margin_level:.1f}% < {self.min_margin_level_percent}% limit"
                return False, reason

        # 7. Daily Peak Equity Drawdown Guard
        if account_info and self._daily_peak_equity > 0:
            current_equity = account_info.get("equity", 0.0)
            drawdown_usd = self._daily_peak_equity - current_equity
            if drawdown_usd > self.max_daily_drawdown_usd:
                self._is_halted = True
                self._halt_reason = f"Daily drawdown ${drawdown_usd:.2f} exceeded limit ${self.max_daily_drawdown_usd:.2f}"
                logger.critical(f"🛑 CIRCUIT BREAKER TRIPPED: {self._halt_reason}")
                get_notifier().send_risk_alert("DRAWDOWN_BREACH", self._halt_reason)
                return False, self._halt_reason

        # 8. Directional USD Factor Concentration Guard (Max Ceiling: 3)
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

            # 9. Carver Net USD Portfolio Exposure Guard
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
        """Return human-readable risk status summary including USD Factor and Daily Governor metrics."""
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
            "today_realized_pnl": self._today_realized_pnl,
            "today_deals_count": self._today_deals_count,
            "daily_profit_target_min": self.daily_profit_target_min,
            "daily_profit_target_max": self.daily_profit_target_max,
            "daily_max_loss_limit": self.daily_max_loss_limit,
            "risk_scale": self.get_risk_scale(),
            "is_profit_locked": self._profit_locked,
            "is_daily_loss_tripped": self._daily_loss_tripped,
        }
