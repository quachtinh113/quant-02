"""
Unified Quant Fleet — Bot 4: US Indices 15-Minute ORB Strategy (exness_usidx_sess)
Magic: 260902 | Symbols: US500m, USTECm | Cadence: 15-Min ORB (14:15 UTC) & Close (20:00 UTC)

Architecture:
  1. Opening Range Definition:
     Captures the first 15-minute bar of the US Cash Open (14:00 - 14:15 UTC) to establish ORB_High and ORB_Low.
  2. Range Sizing Filter:
     If (ORB_High - ORB_Low) > 2.5 * ATR(14) on M15, the opening expansion is overextended.
     The strategy aborts the session (No-Trade) to avoid buying tops or selling bottoms.
  3. Breakout Trigger (14:15 UTC):
     BUY when price breaks above ORB_High; SELL when price breaks below ORB_Low.
  4. Expanded Stop Loss Buffer:
     sl_atr_multiplier = 2.0 (upgraded from 1.5) to absorb opening spread expansion and micro-whipsaws.
  5. Overnight Gap Protection:
     At 20:00 UTC (US Cash Close), all intraday index positions are closed flat.
"""

from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
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
        self.base_lot = config.get("base_lot", 0.01)
        self.base_lots = config.get("base_lots", {"US500": 0.14, "USTEC": 0.05})
        self.sl_atr_mult = config.get("sl_atr_multiplier", 2.0)  # Upgraded to 2.0x ATR buffer
        self.tp_atr_mult = config.get("tp_atr_multiplier", 2.5)
        self.max_orb_atr_ratio = config.get("max_orb_atr_ratio", 2.5)  # Range sizing filter
        self.spread_limit = config.get("spread_limit_points", 500)
        self.orb_minute_start = config.get("orb_minute_start", 15)  # 14:15 UTC
        self.orb_minute_end = config.get("orb_minute_end", 20)
        self._last_processed_date_session = ""

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        if now_utc.weekday() >= 5:  # No trading on weekends
            return False

        today_str = now_utc.strftime("%Y-%m-%d")

        # 1. Cash Open 15-Minute ORB Breakout (14:15 - 14:20 UTC)
        if now_utc.hour == 14 and self.orb_minute_start <= now_utc.minute <= self.orb_minute_end:
            sess_key = f"{today_str}_14"
            if self._last_processed_date_session != sess_key:
                return True

        # 2. Cash Close Intraday Liquidation (20:00 - 20:05 UTC)
        if now_utc.hour == 20 and 0 <= now_utc.minute <= 5:
            sess_key = f"{today_str}_20"
            if self._last_processed_date_session != sess_key:
                return True

        return False

    def calculate_orb_15m(
        self,
        df,
        now_utc: datetime,
        symbol: str
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Locate the first M15 bar of US Cash Open (14:00 - 14:15 UTC).
        Returns: (orb_high, orb_low, orb_range)
        """
        try:
            today_date = now_utc.date()
            orb_bars = df[(df.index.date == today_date) & (df.index.hour == 14) & (df.index.minute == 0)]
            if orb_bars is not None and len(orb_bars) > 0:
                orb_high = float(orb_bars['high'].iloc[-1])
                orb_low = float(orb_bars['low'].iloc[-1])
                return orb_high, orb_low, orb_high - orb_low

            # Fallback to the latest closed M15 candle if timezone offset shifts index
            last_bar = df.iloc[-2]
            h = float(last_bar['high'])
            l = float(last_bar['low'])
            return h, l, h - l
        except Exception as e:
            logger.warning(f"⚠️ [{self.name}] Could not compute 15m ORB for {symbol}: {e}")
            return None, None, None

    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        today_str = now_utc.strftime("%Y-%m-%d")
        self._last_processed_date_session = f"{today_str}_{now_utc.hour}"
        self.last_decision_time = now_utc

        action_name = "US 15-Min ORB Breakout" if now_utc.hour == 14 else "US Cash Close Liquidation"
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

        # At US Open 15-Min ORB (14:15 UTC), evaluate breakout against Opening Range
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

            # Compute ATR(14) on M15
            df['tr'] = np.maximum(
                df['high'] - df['low'],
                np.maximum((df['high'] - df['close'].shift()).abs(), (df['low'] - df['close'].shift()).abs())
            )
            df['atr'] = df['tr'].rolling(14).mean()
            atr = float(df['atr'].iloc[-2])

            # Determine 15-Minute Opening Range
            orb_high, orb_low, orb_range = self.calculate_orb_15m(df, now_utc, symbol)
            if orb_high is None or orb_low is None or atr <= 0:
                continue

            # Range Sizing Filter: If first 15m bar > 2.5x ATR -> Market overextended, cancel session
            if orb_range > (self.max_orb_atr_ratio * atr):
                logger.warning(
                    f"⚠️ [{self.name}] {symbol} 15m ORB Range ({orb_range:.2f}) > {self.max_orb_atr_ratio}x ATR "
                    f"({self.max_orb_atr_ratio * atr:.2f}). Market overextended at open. Skipping session (No-Trade)."
                )
                continue

            current_ask = tick["ask"]
            current_bid = tick["bid"]

            # Breakout Trigger Logic:
            signal = None
            if current_ask > orb_high:
                signal = "BUY"
            elif current_bid < orb_low:
                signal = "SELL"
            else:
                logger.info(
                    f"⏳ [{self.name}] {symbol} Price ({current_bid:.2f}) is inside 15m ORB "
                    f"[{orb_low:.2f} - {orb_high:.2f}] (Range: {orb_range:.2f}). Postponing entry."
                )
                continue

            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            if len(existing) == 0 and signal is not None:
                all_pos = mt5_mgr.get_positions()
                can_open, reason = risk_guard.can_open_position(
                    account_info=account_info,
                    current_position_count=len(all_pos),
                    open_positions=all_pos,
                    new_symbol=symbol,
                    new_direction=signal
                )
                if not can_open:
                    logger.warning(f"⛔ [{self.name}] Risk veto for {symbol}: {reason}")
                    continue

                clean_sym = symbol[:-1] if symbol.endswith("m") else symbol
                base_raw_lot = self.base_lots.get(clean_sym, self.base_lot)
                lot = mt5_mgr.normalize_lot(symbol, base_raw_lot * risk_guard.get_risk_scale())

                sl = current_bid - (atr * self.sl_atr_mult) if signal == "BUY" else current_ask + (atr * self.sl_atr_mult)
                tp = current_ask + (atr * self.tp_atr_mult) if signal == "BUY" else current_bid - (atr * self.tp_atr_mult)

                res = mt5_mgr.open_market_order(
                    symbol=symbol,
                    direction=signal,
                    volume=lot,
                    sl=sl,
                    tp=tp,
                    magic=self.magic,
                    comment=f"USIDX_ORB15 {signal} (SL:{self.sl_atr_mult}x)"
                )
                if res.get("success"):
                    actions.append(f"OPEN {symbol} {signal} {lot}L (ORB [{orb_low:.1f}-{orb_high:.1f}], SL {self.sl_atr_mult}x)")

        self.last_action = ", ".join(actions) if actions else "HOLD"
