"""
Unified Quant Fleet — Bot 1: Bitcoin 8-Hour Strategy (exness_btc_8h)
Magic: 260904 | Symbol: BTCUSDm | Cadence: 8-Hour (00:00, 08:00, 16:00 UTC)
"""

from datetime import datetime, timezone
from typing import Dict, Any
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


class Btc8hStrategy(BaseStrategy):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(name="BTC_8H", magic=config.get("magic", 260904), config=config)
        self.symbol = config.get("symbol", "BTCUSDm")
        self.decision_hours = config.get("decision_utc_hours", [0, 8, 16])
        self.base_lot = config.get("base_lot", 0.01)
        self.sl_atr_mult = config.get("sl_atr_multiplier", 2.0)
        self.tp_atr_mult = config.get("tp_atr_multiplier", 3.0)
        self.spread_limit = config.get("spread_limit_points", 2000)
        self._last_processed_hour = -1

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        # Run within 5 minutes past the decision hours (00:01-00:05, 08:01-08:05, 16:01-16:05 UTC)
        if now_utc.hour in self.decision_hours and 0 <= now_utc.minute <= 5:
            if self._last_processed_hour != now_utc.hour:
                return True
        return False

    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        self._last_processed_hour = now_utc.hour
        self.last_decision_time = now_utc
        logger.info(f"⚡ [{self.name}] Running 8-Hour Decision Cycle for {self.symbol} @ {now_utc.strftime('%H:%M')} UTC")

        self.sync_positions(mt5_mgr)

        # Check spread
        tick = mt5_mgr.get_tick(self.symbol)
        spec = mt5_mgr.get_symbol_spec(self.symbol)
        if not tick or not spec:
            self.last_action = "SKIP: No tick/spec"
            return

        spread_points = int(round((tick["ask"] - tick["bid"]) / spec["point"]))
        if spread_points > self.spread_limit:
            logger.warning(f"⚠️ [{self.name}] Spread {spread_points} > limit {self.spread_limit}. Skipping.")
            self.last_action = f"SKIP: High Spread ({spread_points} pts)"
            return

        # Fetch H4 bars (8H folded from H4)
        df = mt5_mgr.get_rates(self.symbol, "H4", count=60)
        if df is None or len(df) < 30:
            self.last_action = "SKIP: Insufficient data"
            return

        # Technical Indicators: EMA20, EMA50, ATR14
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        df['tr'] = np.maximum(high_low, np.maximum(high_close, low_close))
        df['atr'] = df['tr'].rolling(14).mean()

        last_row = df.iloc[-2]  # Closed bar
        current_close = last_row['close']
        ema20 = last_row['ema20']
        ema50 = last_row['ema50']
        atr = last_row['atr']

        # Carver Continuous EWMAC Forecast & Multiplier (Mentored by pysystemtrade)
        from core.carver_framework import CarverForecastEngine
        raw_forecast = CarverForecastEngine.calculate_ewmac_forecast(ema20, ema50, atr, scalar=4.0, cap=20.0)
        mult, signal = CarverForecastEngine.get_position_multiplier(
            raw_forecast,
            deadband=self.config.get("forecast_deadband", 3.0),
            target_forecast=10.0,
            max_multiplier=2.0
        )

        logger.info(
            f"📊 [{self.name}] Close={current_close:.2f}, EMA20={ema20:.2f}, EMA50={ema50:.2f}, ATR={atr:.2f} | "
            f"Carver Forecast: {raw_forecast:+.2f} => Multiplier: {mult:.2f}x ({signal})"
        )

        # Position Management
        if len(self.open_positions) > 0:
            # Check for reversal exit
            for pos in self.open_positions:
                if (pos["type"] == "BUY" and signal == "SELL") or (pos["type"] == "SELL" and signal == "BUY") or signal == "FLAT":
                    logger.info(f"🔄 [{self.name}] Signal reversal or exit. Closing position #{pos['ticket']}")
                    mt5_mgr.close_position(pos["ticket"])
            self.sync_positions(mt5_mgr)

        # Open New Position if no existing position and conviction holds
        if len(self.open_positions) == 0 and signal in ("BUY", "SELL") and mult > 0:
            all_positions = mt5_mgr.get_positions()
            can_open, reason = risk_guard.can_open_position(
                account_info=account_info,
                current_position_count=len(all_positions),
                open_positions=all_positions,
                new_symbol=self.symbol,
                new_direction=signal
            )
            if not can_open:
                logger.warning(f"⛔ [{self.name}] Risk guard vetoed entry: {reason}")
                self.last_action = f"VETO: {reason}"
                return

            price = tick["ask"] if signal == "BUY" else tick["bid"]
            sl = price - (atr * self.sl_atr_mult) if signal == "BUY" else price + (atr * self.sl_atr_mult)
            tp = price + (atr * self.tp_atr_mult) if signal == "BUY" else price - (atr * self.tp_atr_mult)

            # Carver Volatility-Targeted Lot Sizing
            equity = account_info.get("equity", 1000.0)
            vol_target = self.config.get("annual_vol_target_percent", 15.0) / 100.0
            base_vol_lot = mt5_mgr.calculate_vol_targeted_lot(
                symbol=self.symbol,
                atr=atr,
                equity=equity,
                annual_vol_target=vol_target,
                num_slots=5,
                min_lot=self.base_lot,
                max_lot=self.config.get("max_lot", 0.05)
            )
            final_lot = mt5_mgr.normalize_lot(self.symbol, base_vol_lot * mult)

            res = mt5_mgr.open_market_order(
                symbol=self.symbol,
                direction=signal,
                volume=final_lot,
                sl=sl,
                tp=tp,
                magic=self.magic,
                comment=f"BTC_8H Carver {raw_forecast:+.1f}"
            )
            if res.get("success"):
                self.last_action = f"OPEN {signal} {final_lot}L #{res['ticket']}"
            else:
                self.last_action = f"ERROR: {res.get('error')}"
        else:
            self.last_action = f"HOLD ({len(self.open_positions)} active, signal={signal})"
