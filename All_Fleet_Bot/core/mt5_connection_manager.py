"""
Unified Quant Fleet — MT5 Connection Manager
Central singleton connector for MetaTrader 5 on 1 PC.
Handles connection health, auto-reconnection, data feeds, and order routing.
"""

import os
import time
import math
import logging
import threading
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False

from core.audit_logger import get_logger

logger = get_logger()

# Timeframe mapping
TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1 if MT5_AVAILABLE else 1,
    "M5": mt5.TIMEFRAME_M5 if MT5_AVAILABLE else 5,
    "M15": mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 15,
    "M30": mt5.TIMEFRAME_M30 if MT5_AVAILABLE else 30,
    "H1": mt5.TIMEFRAME_H1 if MT5_AVAILABLE else 16385,
    "H4": mt5.TIMEFRAME_H4 if MT5_AVAILABLE else 16388,
    "D1": mt5.TIMEFRAME_D1 if MT5_AVAILABLE else 16408,
}


class MT5ConnectionManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MT5ConnectionManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, terminal_path: str = "", account: int = 0, password: str = "", server: str = ""):
        if self._initialized:
            return

        self.terminal_path = terminal_path or os.getenv("MT5_PATH", "")
        self.account = account or int(os.getenv("MT5_ACCOUNT", "0"))
        self.password = password or os.getenv("MT5_PASSWORD", "")
        self.server = server or os.getenv("MT5_SERVER", "")
        self.connected = False
        self._last_health_check = 0.0
        self._initialized = True

    def connect(self) -> bool:
        """Initialize and login to MetaTrader 5."""
        if not MT5_AVAILABLE:
            logger.error("❌ MetaTrader5 Python library is not installed.")
            return False

        with self._lock:
            # Reset any stale IPC connection before re-initializing
            try:
                mt5.shutdown()
            except Exception:
                pass

            init_kwargs = {}
            if self.terminal_path and os.path.exists(self.terminal_path):
                init_kwargs["path"] = self.terminal_path
            if self.account:
                init_kwargs["login"] = self.account
            if self.password:
                init_kwargs["password"] = self.password
            if self.server:
                init_kwargs["server"] = self.server

            initialized = mt5.initialize(**init_kwargs) if init_kwargs else mt5.initialize()
            if not initialized:
                initialized = mt5.initialize()

            if not initialized:
                err = mt5.last_error()
                logger.error(f"❌ MT5 initialize() failed: {err}")
                self.connected = False
                return False

            acct_info = mt5.account_info()
            if self.account and self.password and self.server:
                if not (acct_info and acct_info.login == self.account):
                    login_ok = mt5.login(login=self.account, password=self.password, server=self.server)
                    if not login_ok:
                        err = mt5.last_error()
                        logger.error(f"❌ MT5 login({self.account} @ {self.server}) failed: {err}")
                        self.connected = False
                        return False
                    acct_info = mt5.account_info()

            term_info = mt5.terminal_info()
            if not term_info or not acct_info:
                logger.warning("⚠️ MT5 initialized but terminal_info / account_info unavailable.")
                self.connected = False
                return False

            self.connected = True
            logger.info(
                f"✅ MT5 Connected: Account {acct_info.login} ({acct_info.server}), "
                f"Balance: ${acct_info.balance:.2f}, Equity: ${acct_info.equity:.2f}, Leverage: 1:{acct_info.leverage}"
            )
            return True

    def ensure_connected(self) -> bool:
        """Check connection health and auto-reconnect if needed."""
        now = time.time()
        if now - self._last_health_check < 5.0 and self.connected:
            return True

        self._last_health_check = now
        if not MT5_AVAILABLE:
            return False

        try:
            term_info = mt5.terminal_info()
            if term_info and term_info.connected:
                self.connected = True
                return True
        except Exception:
            pass

        logger.warning("⚠️ MT5 connection dropped. Attempting reconnect...")
        return self.connect()

    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """Retrieve latest account snapshot."""
        if not self.ensure_connected():
            return None

        with self._lock:
            info = mt5.account_info()
            if not info:
                return None
            return {
                "login": info.login,
                "server": info.server,
                "balance": float(info.balance),
                "equity": float(info.equity),
                "margin": float(info.margin),
                "free_margin": float(info.margin_free),
                "margin_level": float(info.margin_level) if info.margin_level else 0.0,
                "profit": float(info.profit),
                "currency": info.currency,
                "leverage": info.leverage,
            }

    def resolve_symbol(self, symbol: str) -> str:
        """Resolve broker symbol name with or without 'm' suffix."""
        if not MT5_AVAILABLE:
            return symbol
        if mt5.symbol_info(symbol):
            return symbol
        alt = symbol[:-1] if symbol.endswith('m') else symbol + 'm'
        if mt5.symbol_info(alt):
            return alt
        return symbol

    def _get_filling_mode(self, symbol: str) -> int:
        """Auto-detect the supported filling mode for a given symbol."""
        if not MT5_AVAILABLE:
            return 0
        info = mt5.symbol_info(symbol)
        if not info:
            return mt5.ORDER_FILLING_IOC
        # Check bitmask flags
        if info.filling_mode & 2:  # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        if info.filling_mode & 1:  # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def get_rates(self, symbol: str, timeframe_str: str, count: int = 100) -> Optional[pd.DataFrame]:
        """Fetch latest candles for a symbol. Returns DataFrame with UTC datetime index."""
        if not self.ensure_connected():
            return None

        tf = TIMEFRAME_MAP.get(timeframe_str.upper(), mt5.TIMEFRAME_H1)
        symbol = self.resolve_symbol(symbol)

        with self._lock:
            if not mt5.symbol_select(symbol, True):
                logger.error(f"❌ Cannot select symbol '{symbol}' in Market Watch.")
                return None

            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None or len(rates) == 0:
                logger.error(f"❌ Failed to fetch rates for {symbol} ({timeframe_str}): {mt5.last_error()}")
                return None

            df = pd.DataFrame(rates)
            df['datetime'] = pd.to_datetime(df['time'], unit='s', utc=True)
            df.set_index('datetime', inplace=True)
            return df

    def get_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch latest tick for a symbol."""
        if not self.ensure_connected():
            return None

        symbol = self.resolve_symbol(symbol)
        with self._lock:
            mt5.symbol_select(symbol, True)
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                return None
            return {
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "spread": float(tick.ask - tick.bid),
                "time": tick.time,
            }

    def get_symbol_spec(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieve instrument specifications (point, digits, contract_size, lot steps)."""
        if not self.ensure_connected():
            return None

        symbol = self.resolve_symbol(symbol)
        with self._lock:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
            if not info:
                return None
            return {
                "point": float(info.point),
                "digits": int(info.digits),
                "contract_size": float(info.trade_contract_size),
                "volume_min": float(info.volume_min),
                "volume_max": float(info.volume_max),
                "volume_step": float(info.volume_step),
                "spread_points": int(info.spread),
            }

    def normalize_lot(self, symbol: str, lot: float) -> float:
        """Round volume down to the instrument's volume_step and clamp between min and max."""
        spec = self.get_symbol_spec(symbol)
        if not spec:
            return round(lot, 2)

        vol_min = spec["volume_min"]
        vol_max = spec["volume_max"]
        vol_step = spec["volume_step"]

        steps = math.floor((lot - vol_min) / vol_step)
        norm = vol_min + (steps * vol_step)
        norm = max(vol_min, min(norm, vol_max))

        # Precision from step
        decimals = 2
        if vol_step >= 1:
            decimals = 0
        elif vol_step >= 0.1:
            decimals = 1
        elif vol_step >= 0.01:
            decimals = 2
        elif vol_step >= 0.001:
            decimals = 3

        return round(norm, decimals)

    def calculate_vol_targeted_lot(
        self,
        symbol: str,
        atr: float,
        equity: float,
        annual_vol_target: float = 0.15,
        num_slots: int = 5,
        min_lot: Optional[float] = None,
        max_lot: Optional[float] = None
    ) -> float:
        """
        Calculate volatility-calibrated lot size using Carver Vol Sizing.
        Reference: pysystemtrade/systems/positionsizing.py
        """
        spec = self.get_symbol_spec(symbol)
        if not spec:
            return min_lot or 0.01

        from core.carver_framework import CarverVolSizer
        return CarverVolSizer.calculate_lot(
            symbol=symbol,
            equity=equity,
            atr=atr,
            spec=spec,
            annual_vol_target=annual_vol_target,
            num_slots=num_slots,
            min_lot_override=min_lot,
            max_lot_override=max_lot
        )

    def get_positions(self, magic: Optional[int] = None, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve active open positions filtered by magic and/or symbol."""
        if not self.ensure_connected():
            return []

        with self._lock:
            raw = mt5.positions_get()
            if raw is None:
                return []

            positions = []
            for p in raw:
                if magic is not None and p.magic != magic:
                    continue
                if symbol is not None and p.symbol != symbol:
                    continue
                positions.append({
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                    "volume": float(p.volume),
                    "price_open": float(p.price_open),
                    "price_current": float(p.price_current),
                    "sl": float(p.sl),
                    "tp": float(p.tp),
                    "profit": float(p.profit),
                    "magic": int(p.magic),
                    "comment": str(p.comment),
                    "time": datetime.fromtimestamp(p.time, tz=timezone.utc),
                })
            return positions

    def get_history_deals(
        self,
        date_from: datetime,
        date_to: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve closed/realized deals within a datetime range."""
        if not self.ensure_connected():
            return []

        if date_to is None:
            date_to = datetime.now(timezone.utc)

        t_from = int(date_from.timestamp()) if hasattr(date_from, 'timestamp') else date_from
        t_to = int(date_to.timestamp()) if hasattr(date_to, 'timestamp') else date_to

        with self._lock:
            deals = mt5.history_deals_get(t_from, t_to)
            if deals is None:
                return []

            records = []
            for d in deals:
                # Exclude balance adjustment transactions (DEAL_TYPE_BALANCE = 2)
                if getattr(d, 'type', 0) == 2:
                    continue
                records.append({
                    "ticket": d.ticket,
                    "order": d.order,
                    "time": d.time,
                    "symbol": d.symbol,
                    "type": "BUY" if d.type == 0 else "SELL",
                    "entry": d.entry,
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "swap": float(d.swap),
                    "commission": float(d.commission),
                    "net_profit": float(d.profit + d.swap + d.commission),
                    "magic": int(getattr(d, 'magic', 0)),
                    "comment": str(getattr(d, 'comment', ""))
                })
            return records

    def open_market_order(
        self,
        symbol: str,
        direction: str,  # "BUY" or "SELL"
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
        magic: int = 0,
        comment: str = ""
    ) -> Dict[str, Any]:
        """Send an IOC market order to MT5."""
        if not self.ensure_connected():
            return {"success": False, "error": "Not connected to MT5"}

        spec = self.get_symbol_spec(symbol)
        if not spec:
            return {"success": False, "error": f"Invalid symbol {symbol}"}

        lot = self.normalize_lot(symbol, volume)
        tick = self.get_tick(symbol)
        if not tick:
            return {"success": False, "error": f"Failed to get tick for {symbol}"}

        symbol = self.resolve_symbol(symbol)
        order_type = mt5.ORDER_TYPE_BUY if direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick["ask"] if direction.upper() == "BUY" else tick["bid"]

        # Sanitize and validate SL and TP to prevent Retcode 10016 (Invalid Stops)
        point = spec.get("point", 0.01)
        spread_dist = max(spec.get("spread_points", 10), 10) * point

        valid_sl = 0.0
        if sl > 0:
            if direction.upper() == "BUY":
                max_sl = tick["bid"] - spread_dist
                valid_sl = min(sl, max_sl)
            else:
                min_sl = tick["ask"] + spread_dist
                valid_sl = max(sl, min_sl)
            valid_sl = round(valid_sl, spec["digits"])

        valid_tp = 0.0
        if tp > 0:
            if direction.upper() == "BUY":
                min_tp = tick["ask"] + spread_dist
                valid_tp = max(tp, min_tp)
            else:
                max_tp = tick["bid"] - spread_dist
                valid_tp = min(tp, max_tp)
            valid_tp = round(valid_tp, spec["digits"])

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": valid_sl,
            "tp": valid_tp,
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(symbol),
        }

        with self._lock:
            t_start = time.perf_counter()
            result = mt5.order_send(request)
            latency_ms = (time.perf_counter() - t_start) * 1000.0

            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                err_msg = result.comment if result else str(mt5.last_error())
                retcode = result.retcode if result else -1
                logger.error(f"❌ Order Send Failed ({symbol} {direction} {lot}L, magic={magic}): {err_msg} (retcode={retcode})")
                return {"success": False, "error": err_msg, "retcode": retcode}

            logger.info(
                f"🚀 ORDER EXECUTED: Ticket #{result.order} | {symbol} {direction} {lot}L @ {result.price} | Magic: {magic}"
            )

            # Record Execution Audit (Slippage, Latency, Spread)
            try:
                from core.execution_audit import get_execution_auditor
                point_val = spec.get("point", 0.0001)
                spread_pts = float(tick.get("spread", 0.0) / point_val) if point_val > 0 else 0.0
                get_execution_auditor().record_execution(
                    order_ticket=result.order,
                    deal_ticket=getattr(result, 'deal', result.order),
                    symbol=symbol,
                    direction=direction,
                    volume=lot,
                    magic=magic,
                    requested_price=price,
                    executed_price=result.price,
                    point=point_val,
                    digits=spec.get("digits", 2),
                    latency_ms=latency_ms,
                    spread_points=spread_pts,
                    comment=comment
                )
            except Exception as audit_err:
                logger.warning(f"⚠️ [EXECUTION AUDIT] Could not record execution: {audit_err}")

            try:
                from core.telegram_notifier import get_notifier
                get_notifier().send_order_open(
                    ticket=result.order,
                    symbol=symbol,
                    order_type=direction,
                    lot=lot,
                    price=result.price,
                    sl=valid_sl,
                    tp=valid_tp,
                    magic=magic,
                    note=comment
                )
            except Exception:
                pass

            return {
                "success": True,
                "ticket": result.order,
                "price": result.price,
                "volume": result.volume,
                "magic": magic
            }

    def modify_position_sltp(self, ticket: int, sl: float, tp: float = 0.0) -> Dict[str, Any]:
        """Modify SL and TP for an open position with strict stop sanitization."""
        if not self.ensure_connected():
            return {"success": False, "error": "Not connected to MT5"}

        with self._lock:
            positions = mt5.positions_get(ticket=ticket)
            if not positions or len(positions) == 0:
                return {"success": False, "error": f"Position #{ticket} not found"}

            pos = positions[0]
            symbol = pos.symbol
            spec = self.get_symbol_spec(symbol)
            tick = self.get_tick(symbol)
            if not spec or not tick:
                return {"success": False, "error": f"Cannot get spec/tick for {symbol}"}

            point = spec.get("point", 0.01)
            spread_dist = max(spec.get("spread_points", 10), 10) * point
            digits = spec.get("digits", 2)
            direction = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"

            valid_sl = 0.0
            if sl > 0:
                if direction == "BUY":
                    max_sl = tick["bid"] - spread_dist
                    valid_sl = min(sl, max_sl)
                else:
                    min_sl = tick["ask"] + spread_dist
                    valid_sl = max(sl, min_sl)
                valid_sl = round(valid_sl, digits)

            valid_tp = round(tp, digits) if tp > 0 else float(pos.tp)

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": pos.ticket,
                "symbol": symbol,
                "sl": valid_sl,
                "tp": valid_tp
            }

            result = mt5.order_send(request)
            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                err_msg = result.comment if result else str(mt5.last_error())
                retcode = result.retcode if result else -1
                return {"success": False, "error": err_msg, "retcode": retcode}

            logger.info(f"🛡️ [SLTP MODIFIED] Ticket #{ticket} ({symbol}) new SL={valid_sl}, TP={valid_tp}")
            return {"success": True, "ticket": ticket, "sl": valid_sl, "tp": valid_tp}

    def close_position(self, ticket: int) -> Dict[str, Any]:
        """Close an open position by ticket using an opposite deal."""
        if not self.ensure_connected():
            return {"success": False, "error": "Not connected to MT5"}

        with self._lock:
            positions = mt5.positions_get(ticket=ticket)
            if not positions or len(positions) == 0:
                return {"success": False, "error": f"Position #{ticket} not found"}

            pos = positions[0]
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                return {"success": False, "error": f"Failed to get tick for {pos.symbol}"}

            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": pos.ticket,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": close_type,
                "price": price,
                "deviation": 20,
                "magic": pos.magic,
                "comment": "Fleet Close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(pos.symbol),
            }

            result = mt5.order_send(request)
            if not result or result.retcode != mt5.TRADE_RETCODE_DONE:
                err_msg = result.comment if result else str(mt5.last_error())
                logger.error(f"❌ Close Failed #{ticket}: {err_msg}")
                return {"success": False, "error": err_msg}

            logger.info(f"✅ POSITION CLOSED: Ticket #{ticket} ({pos.symbol} {pos.volume}L, Profit: ${pos.profit:.2f})")
            try:
                from core.telegram_notifier import get_notifier
                get_notifier().send_order_close(
                    ticket=ticket,
                    symbol=pos.symbol,
                    profit=float(pos.profit),
                    reason="Fleet Close"
                )
            except Exception:
                pass

            return {"success": True, "ticket": ticket, "profit": pos.profit}

    def shutdown(self):
        """Cleanly shutdown MT5 connection."""
        with self._lock:
            if MT5_AVAILABLE and self.connected:
                mt5.shutdown()
                self.connected = False
                logger.info("🔌 MT5 connection shut down cleanly.")
