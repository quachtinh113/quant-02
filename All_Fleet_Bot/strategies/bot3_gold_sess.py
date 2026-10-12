"""
Unified Quant Fleet — Bot 3: Gold Session Strategy (exness_gold_sess)
Magic: 260903 | Symbols: XAUUSDm, XAGUSDm | Cadence: London & NY Open Sessions
"""

from datetime import datetime
from typing import Dict, Any, List
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


class GoldSessStrategy(BaseStrategy):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(name="GOLD_SESS", magic=config.get("magic", 260903), config=config)
        self.symbols = config.get("symbols", ["XAUUSDm", "XAGUSDm"])
        self.session_hours = [8, 13]  # 08:00 UTC (London), 13:00 UTC (New York)
        self.base_lot = config.get("base_lot", 0.01)
        self.sl_atr_mult = config.get("sl_atr_multiplier", 2.0)
        self.tp_atr_mult = config.get("tp_atr_multiplier", 2.5)
        self.spread_limit = config.get("spread_limit_points", 60)
        self._last_processed_session = -1

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        # Only trade Mon-Fri
        if now_utc.weekday() >= 5:
            return False

        if now_utc.hour in self.session_hours and 0 <= now_utc.minute <= 5:
            if self._last_processed_session != now_utc.hour:
                return True
        return False

    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        self._last_processed_session = now_utc.hour
        self.last_decision_time = now_utc
        sess_name = "London" if now_utc.hour == 8 else "New York"
        logger.info(f"⚡ [{self.name}] Running {sess_name} Session Cycle @ {now_utc.strftime('%H:%M')} UTC")

        self.sync_positions(mt5_mgr)
        actions = []

        for symbol in self.symbols:
            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            if not spec or not tick:
                continue

            spread_points = int(round((tick["ask"] - tick["bid"]) / spec["point"]))
            if spread_points > self.spread_limit:
                logger.warning(f"⚠️ [{self.name}] {symbol} Spread {spread_points} > limit {self.spread_limit}. Skipping.")
                continue

            # Fetch H1 bars
            df = mt5_mgr.get_rates(symbol, "H1", count=40)
            if df is None or len(df) < 20:
                continue

            # Compute Asian Range (00:00 to 07:00 UTC)
            df['tr'] = np.maximum(
                df['high'] - df['low'],
                np.maximum((df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs())
            )
            df['atr'] = df['tr'].rolling(14).mean()

            last_bar = df.iloc[-2]
            current_price = tick["bid"]
            atr = last_bar['atr']

            # Session momentum check: EMA9 vs EMA21 on H1 via Carver EWMAC
            ema9 = df['close'].ewm(span=9, adjust=False).mean().iloc[-2]
            ema21 = df['close'].ewm(span=21, adjust=False).mean().iloc[-2]

            from core.carver_framework import CarverForecastEngine
            raw_forecast = CarverForecastEngine.calculate_ewmac_forecast(ema9, ema21, atr, scalar=5.0, cap=20.0)
            mult, signal = CarverForecastEngine.get_position_multiplier(
                raw_forecast,
                deadband=self.config.get("forecast_deadband", 3.0),
                target_forecast=10.0,
                max_multiplier=2.0
            )

            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            if len(existing) == 0 and signal in ("BUY", "SELL") and mult > 0:
                all_positions = mt5_mgr.get_positions()
                can_open, reason = risk_guard.can_open_position(
                    account_info=account_info,
                    current_position_count=len(all_positions),
                    open_positions=all_positions,
                    new_symbol=symbol,
                    new_direction=signal
                )
                if not can_open:
                    logger.warning(f"⛔ [{self.name}] Risk veto for {symbol}: {reason}")
                    continue

                price = tick["ask"] if signal == "BUY" else tick["bid"]
                sl = price - (atr * self.sl_atr_mult) if signal == "BUY" else price + (atr * self.sl_atr_mult)
                tp = price + (atr * self.tp_atr_mult) if signal == "BUY" else price - (atr * self.tp_atr_mult)

                # Carver Volatility-Targeted Lot Sizing
                equity = account_info.get("equity", 1000.0)
                vol_target = self.config.get("annual_vol_target_percent", 15.0) / 100.0
                base_vol_lot = mt5_mgr.calculate_vol_targeted_lot(
                    symbol=symbol,
                    atr=atr,
                    equity=equity,
                    annual_vol_target=vol_target,
                    num_slots=5,
                    min_lot=self.base_lot,
                    max_lot=self.config.get("max_lot", 0.03)
                )
                final_lot = mt5_mgr.normalize_lot(symbol, base_vol_lot * mult)

                res = mt5_mgr.open_market_order(
                    symbol=symbol,
                    direction=signal,
                    volume=final_lot,
                    sl=sl,
                    tp=tp,
                    magic=self.magic,
                    comment=f"GOLD_{sess_name} {raw_forecast:+.1f}"
                )
                if res.get("success"):
                    actions.append(f"OPEN {symbol} {signal} {final_lot}L")

        self.last_action = ", ".join(actions) if actions else "HOLD"
