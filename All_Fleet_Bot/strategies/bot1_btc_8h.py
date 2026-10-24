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

    def calculate_volatility_compression(self, df) -> Dict[str, Any]:
        """
        Quantify volatility squeeze using Bollinger Bands Width (BBW) and ATR Percentile.
        Detects low-volatility coiling (compression) in bottom 15% range.
        Returns: {
            'is_squeeze': bool,
            'bbw': float,
            'bbw_threshold': float,
            'atr_percentile': float,
            'status': str
        }
        """
        try:
            rolling_mean = df['close'].rolling(20).mean()
            rolling_std = df['close'].rolling(20).std()
            upper_band = rolling_mean + (2.0 * rolling_std)
            lower_band = rolling_mean - (2.0 * rolling_std)
            bbw = (upper_band - lower_band) / (rolling_mean + 1e-9)

            if 'atr' in df.columns:
                atr_series = df['atr']
            else:
                high_low = df['high'] - df['low']
                high_close = (df['high'] - df['close'].shift()).abs()
                low_close = (df['low'] - df['close'].shift()).abs()
                tr = np.maximum(high_low, np.maximum(high_close, low_close))
                atr_series = tr.rolling(14).mean()

            min_atr = atr_series.rolling(30).min()
            max_atr = atr_series.rolling(30).max()
            atr_percentile = (atr_series - min_atr) / (max_atr - min_atr + 1e-9)

            cur_bbw = float(bbw.iloc[-2]) if len(bbw) >= 2 else 0.05
            cur_atr_pctl = float(atr_percentile.iloc[-2]) if len(atr_percentile) >= 2 else 0.50

            bbw_q15 = float(bbw.rolling(30).quantile(0.15).iloc[-2]) if len(bbw) >= 30 else 0.03
            is_squeeze = (cur_bbw <= bbw_q15) or (cur_atr_pctl <= 0.15)

            return {
                "is_squeeze": bool(is_squeeze),
                "bbw": cur_bbw,
                "bbw_threshold": bbw_q15,
                "atr_percentile": cur_atr_pctl,
                "status": "PRE_BREAKOUT_SQUEEZE" if is_squeeze else "NORMAL"
            }
        except Exception as e:
            logger.warning(f"⚠️ [{self.name}] Error calculating volatility compression: {e}")
            return {
                "is_squeeze": False,
                "bbw": 0.05,
                "bbw_threshold": 0.03,
                "atr_percentile": 0.50,
                "status": "ERROR_FALLBACK"
            }

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

        # Volatility Squeeze & Compression Scanner
        compression = self.calculate_volatility_compression(df)
        if compression["is_squeeze"]:
            logger.warning(
                f"⚡ [{self.name}] PRE-BREAKOUT SQUEEZE DETECTED: BBW={compression['bbw']:.4f} "
                f"(Threshold: {compression['bbw_threshold']:.4f}), ATR%={compression['atr_percentile']:.1%}. "
                f"Armed in Pre-Breakout Alert mode."
            )
            try:
                from core.telegram_notifier import get_notifier
                get_notifier().send_message(
                    f"⚡ *[BTC_8H VOLATILITY SQUEEZE DETECTED]*\n"
                    f"• *Symbol:* `{self.symbol}` | *Price:* `{current_close:.2f}`\n"
                    f"• *BBW:* `{compression['bbw']:.4f}` | *ATR Percentile:* `{compression['atr_percentile']:.1%}`\n"
                    f"• *Trạng thái:* Thị trường nén biên độ sâu! Sẵn sàng đón đầu sóng bứt phá lớn.\n"
                    f"• *Kế hoạch:* Giải ngân Tranche 1 (0.50x Carver Multiplier) ở nến xác nhận đầu tiên."
                )
            except Exception:
                pass

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
            f"BBW={compression['bbw']:.4f} (ATR%={compression['atr_percentile']:.1%}, Squeeze={compression['is_squeeze']}) | "
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

            # Squeeze Tranche Staging: 0.50x initial size during squeeze to avoid 2-way wick fakeouts
            squeeze_scalar = 0.50 if compression["is_squeeze"] else 1.0

            # Cross-Asset Correlation scale from Risk Guard
            corr_scale = risk_guard.get_correlation_scale(all_positions, self.symbol, signal)

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
            raw_target_lot = base_vol_lot * mult * squeeze_scalar * corr_scale * risk_guard.get_risk_scale()
            final_lot = mt5_mgr.normalize_lot(self.symbol, raw_target_lot)

            sqz_label = " [SQZ_T1]" if compression["is_squeeze"] else ""
            res = mt5_mgr.open_market_order(
                symbol=self.symbol,
                direction=signal,
                volume=final_lot,
                sl=sl,
                tp=tp,
                magic=self.magic,
                comment=f"BTC_8H Carver {raw_forecast:+.1f}{sqz_label}"
            )
            if res.get("success"):
                self.last_action = f"OPEN {signal} {final_lot}L #{res['ticket']}{sqz_label}"
            else:
                self.last_action = f"ERROR: {res.get('error')}"
        else:
            self.last_action = f"HOLD ({len(self.open_positions)} active, signal={signal}, sqz={compression['is_squeeze']})"
