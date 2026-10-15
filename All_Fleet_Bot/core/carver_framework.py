"""
=====================================================================
UNIFIED QUANT FLEET — CARVER INSTITUTIONAL FRAMEWORK
Mentored by pysystemtrade (Robert Carver Systematic Trading Architecture)
=====================================================================
Components:
  1. CarverVolSizer: Dynamic Volatility-Targeted Position Sizing
  2. CarverForecastEngine: Continuous Forecast Scaling & Capping [-20, +20]
  3. CarverFactorGuard: Currency & Net USD Factor Exposure Protection
  4. CarverBufferEngine: Order Buffering Bands to prevent overtrading
=====================================================================
"""

import math
from typing import Dict, Any, List, Tuple, Optional
import pandas as pd
import numpy as np


class CarverVolSizer:
    """
    Carver Dynamic Volatility-Targeted Sizing.
    Reference: pysystemtrade/systems/positionsizing.py
    Formula:
        Daily Cash Vol Target = (Capital * Annual Vol Target) / 16.0
        Block Currency Vol = ATR * Contract Size * Tick Value (in USD)
        Target Lot = (Daily Cash Vol Target / Active Slots) / Block Currency Vol
    """
    @staticmethod
    def calculate_lot(
        symbol: str,
        equity: float,
        atr: float,
        spec: Dict[str, Any],
        annual_vol_target: float = 0.15,
        num_slots: int = 5,
        min_lot_override: Optional[float] = None,
        max_lot_override: Optional[float] = None
    ) -> float:
        """
        Calculate volatility-calibrated lot size for an instrument.
        """
        if equity <= 0 or atr <= 0:
            return spec.get("volume_min", 0.01)

        # 16.0 is approx sqrt(256) trading days in a year
        daily_cash_vol_target = (equity * annual_vol_target) / 16.0
        target_vol_per_slot = daily_cash_vol_target / max(1, num_slots)

        contract_size = spec.get("contract_size", 1.0)
        # 1 lot value move per 1 unit of ATR
        block_currency_vol = atr * contract_size

        if block_currency_vol <= 0:
            return spec.get("volume_min", 0.01)

        raw_lot = target_vol_per_slot / block_currency_vol

        # Bounds from instrument spec
        vol_min = spec.get("volume_min", 0.01)
        vol_max = spec.get("volume_max", 10.0)
        vol_step = spec.get("volume_step", 0.01)

        if min_lot_override is not None:
            vol_min = max(vol_min, min_lot_override)
        if max_lot_override is not None:
            vol_max = min(vol_max, max_lot_override)

        # Quantize to step
        steps = round((raw_lot - vol_min) / vol_step) if vol_step > 0 else 0
        norm_lot = vol_min + max(0, steps) * vol_step
        norm_lot = max(vol_min, min(norm_lot, vol_max))

        decimals = 2
        if vol_step >= 1:
            decimals = 0
        elif vol_step >= 0.1:
            decimals = 1
        elif vol_step >= 0.01:
            decimals = 2
        elif vol_step >= 0.001:
            decimals = 3

        return round(norm_lot, decimals)


class CarverForecastEngine:
    """
    Continuous Forecast Scaling & Capping [-20, +20].
    Reference: pysystemtrade/systems/forecast_scale_cap.py
    Rules:
      - Raw signals are standardized so average absolute magnitude is ~10.0
      - Clamped strictly within [-20.0, +20.0]
      - Deadband [-3.0, +3.0] filters out market chop / noise
    """
    @staticmethod
    def scale_and_cap(raw_signal: float, scalar: float = 1.0, cap: float = 20.0) -> float:
        """Scale raw trading rule output and cap at +/- cap."""
        scaled = raw_signal * scalar
        return float(np.clip(scaled, -cap, cap))

    @staticmethod
    def calculate_ewmac_forecast(
        fast_ema: float,
        slow_ema: float,
        atr: float,
        scalar: float = 4.0,
        cap: float = 20.0
    ) -> float:
        """
        Carver EWMAC (Exponentially Weighted Moving Average Crossover) rule:
        (EMA_fast - EMA_slow) / ATR * scalar
        """
        if atr <= 0:
            return 0.0
        raw = (fast_ema - slow_ema) / atr
        return CarverForecastEngine.scale_and_cap(raw, scalar=scalar, cap=cap)

    @staticmethod
    def get_position_multiplier(
        forecast: float,
        deadband: float = 3.0,
        target_forecast: float = 10.0,
        max_multiplier: float = 2.0
    ) -> Tuple[float, str]:
        """
        Map a continuous forecast to a position size multiplier and direction.
        Returns: (multiplier: float, direction: 'BUY' | 'SELL' | 'FLAT')
        """
        if abs(forecast) < deadband:
            return 0.0, "FLAT"

        direction = "BUY" if forecast > 0 else "SELL"
        # Proportionate sizing: 10 forecast = 1.0x lot, 20 forecast = 2.0x lot
        mult = min(max_multiplier, abs(forecast) / target_forecast)
        return round(mult, 2), direction


class CarverFactorGuard:
    """
    Currency & Factor Exposure Protector.
    Reference: pysystemtrade/sysquant/portfolio_risk.py
    Prevents joint factor drawdown when multiple bots simultaneously load up
    on correlated currency directions (specifically US Dollar exposure).
    """

    # Directional USD impact of LONG position on instrument
    # +1: Long USD (e.g. USD/JPY, USD/CAD, USD/CHF)
    # -1: Short USD (e.g. EUR/USD, GBP/USD, XAU/USD, BTC/USD)
    #  0: Neutral / Non-USD cross or equity index
    USD_EXPOSURE_MAP = {
        # USD as base currency (LONG = LONG USD)
        "USDJPY": 1,
        "USDJPYm": 1,
        "USDCAD": 1,
        "USDCADm": 1,
        "USDCHF": 1,
        "USDCHFm": 1,

        # USD as quote currency (LONG = SHORT USD)
        "EURUSD": -1,
        "EURUSDm": -1,
        "GBPUSD": -1,
        "GBPUSDm": -1,
        "AUDUSD": -1,
        "AUDUSDm": -1,
        "NZDUSD": -1,
        "NZDUSDm": -1,
        "XAUUSD": -1,
        "XAUUSDm": -1,
        "XAGUSD": -1,
        "XAGUSDm": -1,
        "BTCUSD": -1,
        "BTCUSDm": -1,

        # Indices - low currency delta, treated as neutral currency factor
        "US500": 0,
        "US500m": 0,
        "USTEC": 0,
        "USTECm": 0,
    }

    @classmethod
    def get_symbol_usd_delta(cls, symbol: str, direction: str) -> int:
        """
        Calculate Net USD delta for a trade.
        Returns +1 (Long USD), -1 (Short USD), or 0 (Neutral).
        """
        base_factor = cls.USD_EXPOSURE_MAP.get(symbol, 0)
        dir_mult = 1 if direction.upper() == "BUY" else -1
        return base_factor * dir_mult

    @classmethod
    def calculate_portfolio_usd_delta(cls, open_positions: List[Dict[str, Any]]) -> int:
        """
        Calculate current aggregate Net USD delta across all open positions.
        """
        net_usd = 0
        for p in open_positions:
            sym = p.get("symbol", "")
            pos_type = p.get("type", "")
            net_usd += cls.get_symbol_usd_delta(sym, pos_type)
        return net_usd

    @classmethod
    def count_directional_usd_positions(
        cls, open_positions: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """
        Count total open positions with Long USD and Short USD exposure.
        Returns: (long_usd_count: int, short_usd_count: int)
        """
        long_count = 0
        short_count = 0
        for p in open_positions:
            sym = p.get("symbol", "")
            pos_type = p.get("type", "")
            delta = cls.get_symbol_usd_delta(sym, pos_type)
            if delta > 0:
                long_count += 1
            elif delta < 0:
                short_count += 1
        return long_count, short_count

    @classmethod
    def check_directional_exposure_limit(
        cls,
        open_positions: List[Dict[str, Any]],
        new_symbol: str,
        new_direction: str,
        max_directional_usd: int = 3
    ) -> Tuple[bool, str]:
        """
        Enforce strict ceiling on directional USD positions (Max: 3).
        Long USD: BUY USDCAD, USDJPY, USDCHF or SELL EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD.
        Short USD: Opposite.
        Returns: (allowed: bool, reason: str)
        """
        trade_delta = cls.get_symbol_usd_delta(new_symbol, new_direction)
        if trade_delta == 0:
            return True, "OK"

        long_count, short_count = cls.count_directional_usd_positions(open_positions)
        if trade_delta > 0 and long_count >= max_directional_usd:
            return False, f"USD Factor Exposure Limit Reached (Max: {max_directional_usd})"
        elif trade_delta < 0 and short_count >= max_directional_usd:
            return False, f"USD Factor Exposure Limit Reached (Max: {max_directional_usd})"

        return True, "OK"

    @classmethod
    def check_exposure_limit(
        cls,
        open_positions: List[Dict[str, Any]],
        new_symbol: str,
        new_direction: str,
        max_net_usd_exposure: int = 4
    ) -> Tuple[bool, str, int]:
        """
        Verify whether adding this trade exceeds the Net USD factor concentration limit.
        Returns (allowed: bool, reason: str, resulting_delta: int).
        """
        current_delta = cls.calculate_portfolio_usd_delta(open_positions)
        trade_delta = cls.get_symbol_usd_delta(new_symbol, new_direction)
        resulting_delta = current_delta + trade_delta

        if abs(resulting_delta) > max_net_usd_exposure:
            side = "LONG" if resulting_delta > 0 else "SHORT"
            reason = (
                f"USD Factor concentration veto: Net USD exposure would reach {resulting_delta:+d} {side} "
                f"(Limit is +/-{max_net_usd_exposure})."
            )
            return False, reason, resulting_delta

        return True, "OK", resulting_delta


class CarverBufferEngine:
    """
    Trading Buffering Bands.
    Reference: pysystemtrade/systems/buffering.py
    Prevents overtrading when position changes are small and not worth the spread cost.
    """
    @staticmethod
    def should_adjust_position(
        current_pos: float,
        target_pos: float,
        buffer_ratio: float = 0.20
    ) -> bool:
        """
        Returns True if the deviation exceeds the buffer threshold.
        """
        if current_pos == 0.0 and target_pos != 0.0:
            return True
        if target_pos == 0.0 and current_pos != 0.0:
            return True

        diff = abs(target_pos - current_pos)
        buffer_size = buffer_ratio * max(abs(target_pos), abs(current_pos))
        return diff > buffer_size
