"""
Unified Quant Fleet — Bot 5: NowTrading 2.1 Multi-Timeframe Strategy (NOWTRAEDING)
Magic: 260905 (Tracks legacy 202500) | Multi-Symbol | MTF RSI (H4, H1, M15) + ADX + ATR DCA
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    # Wilder's smoothing
    for i in range(period, len(series)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = np.maximum(high_low, np.maximum(high_close, low_close))
    return tr.rolling(window=period).mean()


class NowTradingStrategy(BaseStrategy):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(name="NOWTRADING_MTF", magic=config.get("magic", 260905), config=config)
        self.legacy_magic = config.get("legacy_magic", 202500)
        self.symbols = config.get("symbols", ["XAUUSDm", "USDCADm", "USDCHFm", "NZDUSDm", "BTCUSDm", "USDJPYm"])
        self.base_lots = config.get("base_lots", {"FX": 0.02, "GOLD": 0.01, "CRYPTO": 0.01, "INDEX": 0.01})
        self.max_dca_layers = config.get("max_dca_layers", 2)
        self.dca_l1_atr = config.get("dca_layer_1_atr", 2.3)
        self.dca_l2_atr = config.get("dca_layer_2_atr", 3.5)
        self.profit_target_usd = config.get("profit_target_usd", 5.0)
        self.holding_max_hours = config.get("holding_max_hours", 24)
        self.rsi_buy_thresh = config.get("rsi_buy_threshold", 53.0)
        self.rsi_sell_thresh = config.get("rsi_sell_threshold", 47.0)

        self._last_evaluated_hour = -1

    def sync_positions(self, mt5_mgr: MT5ConnectionManager) -> List[Dict[str, Any]]:
        # Sync positions matching BOTH active magic (260905) and legacy magic (202500)
        pos_current = mt5_mgr.get_positions(magic=self.magic)
        pos_legacy = mt5_mgr.get_positions(magic=self.legacy_magic)
        self.open_positions = pos_current + pos_legacy
        return self.open_positions

    def should_run_cycle(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False

        # Evaluates hourly entry gate at the top of every hour (HH:00 - HH:05 UTC)
        if 0 <= now_utc.minute <= 5:
            if self._last_evaluated_hour != now_utc.hour:
                return True
        return False

    def on_tick_update(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        """Real-time monitoring on every master tick: Checks TP $5.00 and max holding duration."""
        if not self.enabled:
            return

        self.sync_positions(mt5_mgr)
        if not self.open_positions:
            return

        # Group positions by symbol
        symbols_with_pos = set(p["symbol"] for p in self.open_positions)
        for sym in symbols_with_pos:
            sym_positions = [p for p in self.open_positions if p["symbol"] == sym]
            total_profit = sum(p["profit"] for p in sym_positions)

            # Rule 1: Basket Take Profit ($5.00)
            if total_profit >= self.profit_target_usd:
                logger.info(
                    f"🎯 [{self.name}] {sym} Profit Target reached (${total_profit:.2f} >= ${self.profit_target_usd:.2f}). "
                    f"Closing {len(sym_positions)} positions..."
                )
                for p in sym_positions:
                    mt5_mgr.close_position(p["ticket"])
                continue

            # Rule 2: Max Holding Time (24h)
            oldest_time = min(p["time"] for p in sym_positions)
            holding_hours = (now_utc - oldest_time).total_seconds() / 3600.0
            if holding_hours >= self.holding_max_hours:
                logger.warning(
                    f"⌛ [{self.name}] {sym} cycle exceeded max holding time ({holding_hours:.1f}h >= {self.holding_max_hours}h). "
                    f"Closing basket (P/L: ${total_profit:.2f})..."
                )
                for p in sym_positions:
                    mt5_mgr.close_position(p["ticket"])

    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        self._last_evaluated_hour = now_utc.hour
        self.last_decision_time = now_utc
        logger.info(f"⚡ [{self.name}] Running Hourly Entry Gate @ {now_utc.strftime('%H:%M')} UTC across {len(self.symbols)} symbols")

        self.sync_positions(mt5_mgr)
        actions = []

        for symbol in self.symbols:
            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            if not spec or not tick:
                continue

            # Fetch multi-timeframe candles: H4, H1, M15
            df_h4 = mt5_mgr.get_rates(symbol, "H4", count=50)
            df_h1 = mt5_mgr.get_rates(symbol, "H1", count=50)
            df_m15 = mt5_mgr.get_rates(symbol, "M15", count=50)

            if df_h4 is None or df_h1 is None or df_m15 is None:
                continue
            if len(df_h4) < 20 or len(df_h1) < 20 or len(df_m15) < 20:
                continue

            # Calculate RSI on all 3 timeframes
            rsi_h4 = calculate_rsi(df_h4['close'], 14).iloc[-2]
            rsi_h1 = calculate_rsi(df_h1['close'], 14).iloc[-2]
            rsi_m15 = calculate_rsi(df_m15['close'], 14).iloc[-2]

            # Calculate ATR on H1 for DCA distance
            atr_h1 = calculate_atr(df_h1, 14).iloc[-2]

            existing = [p for p in self.open_positions if p["symbol"] == symbol]

            # Case A: Existing position -> Check DCA entry
            if len(existing) > 0 and len(existing) < (self.max_dca_layers + 1):
                pos_type = existing[0]["type"]
                last_price = existing[-1]["price_open"]
                current_price = tick["ask"] if pos_type == "BUY" else tick["bid"]

                # Determine required DCA distance
                dca_dist = (self.dca_l1_atr if len(existing) == 1 else self.dca_l2_atr) * atr_h1

                should_dca = False
                if pos_type == "BUY" and (last_price - current_price) >= dca_dist:
                    should_dca = True
                elif pos_type == "SELL" and (current_price - last_price) >= dca_dist:
                    should_dca = True

                if should_dca:
                    can_open, reason = risk_guard.can_open_position(account_info, len(mt5_mgr.get_positions()))
                    if can_open:
                        dca_lot = existing[0]["volume"]  # Fixed lot style
                        res = mt5_mgr.open_market_order(
                            symbol=symbol,
                            direction=pos_type,
                            volume=dca_lot,
                            magic=self.magic,
                            comment=f"NOW_DCA_{len(existing)}"
                        )
                        if res.get("success"):
                            logger.info(f"➕ [{self.name}] Opened DCA Layer {len(existing)} for {symbol} (#{res['ticket']})")
                            actions.append(f"DCA {symbol} #{res['ticket']}")

            # Case B: No existing position -> Check fresh entry
            elif len(existing) == 0:
                signal = None
                if rsi_h4 > self.rsi_buy_thresh and rsi_h1 > self.rsi_buy_thresh and rsi_m15 > 50.0:
                    signal = "BUY"
                elif rsi_h4 < self.rsi_sell_thresh and rsi_h1 < self.rsi_sell_thresh and rsi_m15 < 50.0:
                    signal = "SELL"

                if signal is not None:
                    can_open, reason = risk_guard.can_open_position(account_info, len(mt5_mgr.get_positions()))
                    if not can_open:
                        logger.warning(f"⛔ [{self.name}] Risk veto for {symbol}: {reason}")
                        continue

                    # Lot selection by category
                    category = "FX"
                    if "XAU" in symbol or "XAG" in symbol:
                        category = "GOLD"
                    elif "BTC" in symbol:
                        category = "CRYPTO"
                    elif "US" in symbol and ("500" in symbol or "TEC" in symbol or "30" in symbol):
                        category = "INDEX"

                    lot = self.base_lots.get(category, 0.01)

                    res = mt5_mgr.open_market_order(
                        symbol=symbol,
                        direction=signal,
                        volume=lot,
                        magic=self.magic,
                        comment="NOW_Entry"
                    )
                    if res.get("success"):
                        logger.info(
                            f"🚀 [{self.name}] Entered {symbol} {signal} @ {res['price']} "
                            f"(RSI H4={rsi_h4:.1f}, H1={rsi_h1:.1f}, M15={rsi_m15:.1f})"
                        )
                        actions.append(f"OPEN {symbol} {signal}")

        self.last_action = ", ".join(actions) if actions else "HOLD"
