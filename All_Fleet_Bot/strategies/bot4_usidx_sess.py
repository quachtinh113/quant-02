"""
Unified Quant Fleet — Bot 4: US Indices Session Strategy (exness_usidx_sess)
Magic: 260902 | Symbols: US500m, USTECm | Cadence: US Cash Open & Close Sessions
"""

from datetime import datetime
from typing import Dict, Any, List
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


class UsidxSessStrategy(BaseStrategy):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(name="USIDX_SESS", magic=config.get("magic", 260902), config=config)
        self.symbols = config.get("symbols", ["US500m", "USTECm"])
        self.session_hours = [14, 20]  # 14:00 UTC (US Open), 20:00 UTC (US Close)
        self.base_lot = config.get("base_lot", 0.01)
        self.base_lots = config.get("base_lots", {"US500": 0.14, "USTEC": 0.05})
        self.sl_atr_mult = config.get("sl_atr_multiplier", 1.5)
        self.tp_atr_mult = config.get("tp_atr_multiplier", 2.0)
        self.spread_limit = config.get("spread_limit_points", 500)
        self._last_processed_hour = -1

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        if now_utc.weekday() >= 5:  # No trading on weekends
            return False

        if now_utc.hour in self.session_hours and 0 <= now_utc.minute <= 5:
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
        action_name = "US Cash Open" if now_utc.hour == 14 else "US Cash Close"
        logger.info(f"⚡ [{self.name}] Running {action_name} Cycle @ {now_utc.strftime('%H:%M')} UTC")

        self.sync_positions(mt5_mgr)
        actions = []

        # At US Close (20:00 UTC), close intraday index positions to prevent overnight gap risk
        if now_utc.hour == 20:
            for pos in self.open_positions:
                logger.info(f"🌆 [{self.name}] Closing intraday position #{pos['ticket']} before market close.")
                mt5_mgr.close_position(pos["ticket"])
                actions.append(f"CLOSE #{pos['ticket']}")
            self.last_action = ", ".join(actions) if actions else "FLAT"
            return

        # At US Open (14:00 UTC), evaluate breakout/momentum
        for symbol in self.symbols:
            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            if not spec or not tick:
                continue

            spread_points = int(round((tick["ask"] - tick["bid"]) / spec["point"]))
            if spread_points > self.spread_limit:
                logger.warning(f"⚠️ [{self.name}] {symbol} Spread {spread_points} > limit {self.spread_limit}. Skipping.")
                continue

            df = mt5_mgr.get_rates(symbol, "M15", count=50)
            if df is None or len(df) < 25:
                continue

            # ATR on M15
            df['tr'] = np.maximum(
                df['high'] - df['low'],
                np.maximum((df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs())
            )
            df['atr'] = df['tr'].rolling(14).mean()
            atr = df['atr'].iloc[-2]

            # Pre-market range (high/low of last 8 M15 bars = last 2 hours)
            recent_high = df['high'].iloc[-9:-1].max()
            recent_low = df['low'].iloc[-9:-1].min()
            current_price = tick["bid"]

            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            signal = None
            if current_price > recent_high:
                signal = "BUY"
            elif current_price < recent_low:
                signal = "SELL"

            if len(existing) == 0 and signal is not None:
                can_open, reason = risk_guard.can_open_position(account_info, len(mt5_mgr.get_positions()))
                if not can_open:
                    logger.warning(f"⛔ [{self.name}] Risk veto for {symbol}: {reason}")
                    continue

                clean_sym = symbol[:-1] if symbol.endswith("m") else symbol
                lot = self.base_lots.get(clean_sym, self.base_lot)

                sl = tick["bid"] - (atr * self.sl_atr_mult) if signal == "BUY" else tick["ask"] + (atr * self.sl_atr_mult)
                tp = tick["ask"] + (atr * self.tp_atr_mult) if signal == "BUY" else tick["bid"] - (atr * self.tp_atr_mult)

                res = mt5_mgr.open_market_order(
                    symbol=symbol,
                    direction=signal,
                    volume=lot,
                    sl=sl,
                    tp=tp,
                    magic=self.magic,
                    comment="USIDX_Open"
                )
                if res.get("success"):
                    actions.append(f"OPEN {symbol} {signal}")

        self.last_action = ", ".join(actions) if actions else "HOLD"
