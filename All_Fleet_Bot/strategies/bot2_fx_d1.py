"""
Unified Quant Fleet — Bot 2: Forex Daily Strategy (exness_fx_d1)
Magic: 260901 | Symbols: EURUSDm, GBPUSDm, USDJPYm, AUDUSDm, USDCADm | Cadence: D1 Close (20:00 UTC)
"""

from datetime import datetime
from typing import Dict, Any, List
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


class FxD1Strategy(BaseStrategy):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(name="FX_D1", magic=config.get("magic", 260901), config=config)
        self.symbols = config.get("symbols", ["EURUSDm", "GBPUSDm", "USDJPYm", "AUDUSDm", "USDCADm"])
        self.decision_hour = config.get("decision_utc_hour", 20)
        self.decision_weekdays = config.get("decision_weekdays", [1, 2, 3, 4, 5])
        self.base_lot = config.get("base_lot", 0.02)
        self.sl_atr_mult = config.get("sl_atr_multiplier", 1.5)
        self.tp_atr_mult = config.get("tp_atr_multiplier", 2.5)
        self.spread_limit = config.get("spread_limit_points", 50)
        self._last_processed_date = ""

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        today_str = now_utc.strftime("%Y-%m-%d")
        # Run between 20:01 and 20:10 UTC on allowed weekdays
        if (
            now_utc.weekday() in self.decision_weekdays
            and now_utc.hour == self.decision_hour
            and 0 <= now_utc.minute <= 10
        ):
            if self._last_processed_date != today_str:
                return True
        return False

    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        self._last_processed_date = now_utc.strftime("%Y-%m-%d")
        self.last_decision_time = now_utc
        logger.info(f"⚡ [{self.name}] Running Daily D1 Decision Cycle across {len(self.symbols)} FX pairs @ {now_utc.strftime('%H:%M')} UTC")

        self.sync_positions(mt5_mgr)
        actions_taken = []

        for symbol in self.symbols:
            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            if not spec or not tick:
                continue

            spread_points = int(round((tick["ask"] - tick["bid"]) / spec["point"]))
            if spread_points > self.spread_limit:
                logger.warning(f"⚠️ [{self.name}] {symbol} Spread {spread_points} > limit {self.spread_limit}. Skipping.")
                continue

            # Fetch D1 candles
            df = mt5_mgr.get_rates(symbol, "D1", count=40)
            if df is None or len(df) < 25:
                continue

            # Indicators: SMA20, Donchian Channel, ATR14
            df['sma20'] = df['close'].rolling(20).mean()
            high_low = df['high'] - df['low']
            high_close = (df['high'] - df['close'].shift()).abs()
            low_close = (df['low'] - df['close'].shift()).abs()
            df['tr'] = np.maximum(high_low, np.maximum(high_close, low_close))
            df['atr'] = df['tr'].rolling(14).mean()

            last_bar = df.iloc[-2]  # Completed D1 candle
            close = last_bar['close']
            sma20 = last_bar['sma20']
            atr = last_bar['atr']

            # Check open positions on this symbol
            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            signal = None
            if close > sma20 + (0.5 * atr):
                signal = "BUY"
            elif close < sma20 - (0.5 * atr):
                signal = "SELL"

            if len(existing) > 0:
                for p in existing:
                    # Trailing or reversal check
                    if (p["type"] == "BUY" and signal == "SELL") or (p["type"] == "SELL" and signal == "BUY"):
                        logger.info(f"🔄 [{self.name}] {symbol} Reversal: closing #{p['ticket']}")
                        mt5_mgr.close_position(p["ticket"])
                        actions_taken.append(f"CLOSE #{p['ticket']}")
            elif signal is not None:
                can_open, reason = risk_guard.can_open_position(account_info, len(mt5_mgr.get_positions()))
                if not can_open:
                    logger.warning(f"⛔ [{self.name}] Risk guard vetoed {symbol}: {reason}")
                    break

                price = tick["ask"] if signal == "BUY" else tick["bid"]
                sl = price - (atr * self.sl_atr_mult) if signal == "BUY" else price + (atr * self.sl_atr_mult)
                tp = price + (atr * self.tp_atr_mult) if signal == "BUY" else price - (atr * self.tp_atr_mult)

                res = mt5_mgr.open_market_order(
                    symbol=symbol,
                    direction=signal,
                    volume=self.base_lot,
                    sl=sl,
                    tp=tp,
                    magic=self.magic,
                    comment="FX_D1 Fleet"
                )
                if res.get("success"):
                    actions_taken.append(f"OPEN {symbol} {signal}")

        self.last_action = ", ".join(actions_taken) if actions_taken else "HOLD"
