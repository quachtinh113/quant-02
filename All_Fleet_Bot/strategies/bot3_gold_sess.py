"""
Unified Quant Fleet — Bot 3: Gold & Silver Session Strategy (exness_gold_sess)
Magic: 260903 | Symbols: XAUUSDm, XAGUSDm | Cadence: London & NY Open Sessions
Upgrades:
  - Separate parameter configurations for XAUUSD vs XAGUSD.
  - XAUUSD: sl_atr_multiplier = 2.5, automatic lot risk scaling for constant dollar risk.
  - XAUUSD: Asian Range (00:00 - 07:00 UTC) filter to avoid liquidity traps.
  - XAGUSD: retains proven baseline settings (SL 2.0x ATR, TP 2.5x ATR).
"""

from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.carver_framework import CarverForecastEngine
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
        self.spread_limit = config.get("spread_limit_points", 250)
        self._last_processed_session = -1

        # Distinct configurations per symbol:
        # XAUUSD: Upgraded 2.5x ATR SL, constant dollar risk lot scaling, Asian Range filter
        # XAGUSD: Retain proven baseline settings
        self.symbol_configs = {
            "XAUUSD": {
                "sl_atr_multiplier": 2.5,
                "tp_atr_multiplier": 2.5,
                "baseline_sl_atr": 2.0,
                "check_asian_range": True,
                "spread_limit_points": config.get("spread_limit_points", 250),
            },
            "XAGUSD": {
                "sl_atr_multiplier": 2.0,
                "tp_atr_multiplier": 2.5,
                "baseline_sl_atr": 2.0,
                "check_asian_range": False,
                "spread_limit_points": config.get("spread_limit_points", 250),
            }
        }

    def get_symbol_config(self, symbol: str) -> Dict[str, Any]:
        """Resolve specific configuration for XAUUSD vs XAGUSD (handles 'm' suffix)."""
        clean_sym = symbol.upper()
        if "XAU" in clean_sym:
            cfg = self.symbol_configs["XAUUSD"].copy()
        elif "XAG" in clean_sym:
            cfg = self.symbol_configs["XAGUSD"].copy()
        else:
            cfg = {
                "sl_atr_multiplier": self.sl_atr_mult,
                "tp_atr_multiplier": self.tp_atr_mult,
                "baseline_sl_atr": 2.0,
                "check_asian_range": False,
                "spread_limit_points": self.spread_limit,
            }
        custom = self.config.get("symbol_params", {}).get(symbol, {})
        cfg.update(custom)
        return cfg

    def check_asian_range_breakout(
        self,
        df,
        now_utc: datetime,
        current_price: float,
        symbol: str
    ) -> Tuple[bool, Optional[float], Optional[float]]:
        """
        Check if current price is inside Asian Session Range (00:00 - 07:00 UTC).
        Returns: (is_inside_range: bool, asian_high: float, asian_low: float)
        If inside range, breakout order should be deferred to avoid liquidity traps.
        """
        try:
            today_date = now_utc.date()
            asian_bars = df[(df.index.date == today_date) & (df.index.hour >= 0) & (df.index.hour < 7)]
            if asian_bars is not None and len(asian_bars) > 0:
                asian_high = float(asian_bars['high'].max())
                asian_low = float(asian_bars['low'].min())
                if asian_low <= current_price <= asian_high:
                    return True, asian_high, asian_low
                return False, asian_high, asian_low
        except Exception as e:
            logger.warning(f"⚠️ Could not compute Asian range for {symbol}: {e}")
        return False, None, None

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
            sym_cfg = self.get_symbol_config(symbol)
            sl_atr_mult = sym_cfg["sl_atr_multiplier"]
            tp_atr_mult = sym_cfg["tp_atr_multiplier"]
            baseline_sl_atr = sym_cfg.get("baseline_sl_atr", 2.0)
            spread_limit = sym_cfg.get("spread_limit_points", self.spread_limit)
            check_asian = sym_cfg.get("check_asian_range", False)

            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            if not spec or not tick:
                continue

            spread_points = int(round((tick["ask"] - tick["bid"]) / spec["point"]))
            if spread_points > spread_limit:
                logger.warning(f"⚠️ [{self.name}] {symbol} Spread {spread_points} > limit {spread_limit}. Skipping.")
                continue

            # Fetch H1 bars
            df = mt5_mgr.get_rates(symbol, "H1", count=40)
            if df is None or len(df) < 20:
                continue

            # Compute ATR
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

            raw_forecast = CarverForecastEngine.calculate_ewmac_forecast(ema9, ema21, atr, scalar=5.0, cap=20.0)
            mult, signal = CarverForecastEngine.get_position_multiplier(
                raw_forecast,
                deadband=self.config.get("forecast_deadband", 3.0),
                target_forecast=10.0,
                max_multiplier=2.0
            )

            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            if len(existing) == 0 and signal in ("BUY", "SELL") and mult > 0:
                # Asian Range Filter for XAUUSD: Avoid liquidity traps inside the 00:00 - 07:00 UTC range
                if check_asian:
                    eval_price = tick["ask"] if signal == "BUY" else tick["bid"]
                    inside_asian, a_high, a_low = self.check_asian_range_breakout(df, now_utc, eval_price, symbol)
                    if inside_asian:
                        logger.info(
                            f"⏳ [{self.name}] {symbol} price ({eval_price:.2f}) is inside Asian Range "
                            f"[{a_low:.2f} - {a_high:.2f}] (00:00-07:00 UTC). Postponing breakout entry."
                        )
                        continue

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
                sl = price - (atr * sl_atr_mult) if signal == "BUY" else price + (atr * sl_atr_mult)
                tp = price + (atr * tp_atr_mult) if signal == "BUY" else price - (atr * tp_atr_mult)

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

                # Constant Dollar Risk Lot Scaling:
                # Lot is scaled by (baseline_sl_atr / sl_atr_mult) so total dollar risk
                # remains strictly identical even though stop distance increases to 2.5 ATR.
                lot_risk_scalar = baseline_sl_atr / sl_atr_mult if sl_atr_mult > 0 else 1.0
                adjusted_lot = base_vol_lot * mult * lot_risk_scalar
                final_lot = mt5_mgr.normalize_lot(symbol, adjusted_lot)

                res = mt5_mgr.open_market_order(
                    symbol=symbol,
                    direction=signal,
                    volume=final_lot,
                    sl=sl,
                    tp=tp,
                    magic=self.magic,
                    comment=f"GOLD_{sess_name} {raw_forecast:+.1f} (SL:{sl_atr_mult}x)"
                )
                if res.get("success"):
                    actions.append(f"OPEN {symbol} {signal} {final_lot}L (SL {sl_atr_mult}x)")

        self.last_action = ", ".join(actions) if actions else "HOLD"
