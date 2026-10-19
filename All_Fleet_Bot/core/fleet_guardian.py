"""
Unified Quant Fleet — Position Guardian & Two-Stage Auto-Breakeven Engine
Monitors all open positions across the entire fleet.
Features:
  1. Real-time R-Multiple tracking with ATR-based initial risk fallback.
  2. Stage 1 (Breakeven Lock): When Current_R >= 1.5, moves SL to Open_Price +/- (Spread + Slippage).
  3. Stage 2 (Profit Protection): When Current_R >= 2.0, trails SL to lock at least +0.75R or H1 structure.
  4. Non-blocking Telegram telemetry alerts and broker StopLevel sanitization.
"""

import math
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import MetaTrader5 as mt5
import numpy as np

from core.audit_logger import get_logger
from core.mt5_connection_manager import MT5ConnectionManager
from core.telegram_notifier import get_notifier
from config.magic_registry import (
    MAGIC_BTC_8H, MAGIC_FX_D1, MAGIC_GOLD_SESS, MAGIC_USIDX_SESS, MAGIC_NOWTRADING_MTF
)

logger = get_logger()

KNOWN_MAGICS = {
    MAGIC_BTC_8H,
    MAGIC_FX_D1,
    MAGIC_GOLD_SESS,
    MAGIC_USIDX_SESS,
    MAGIC_NOWTRADING_MTF,
    202500  # Legacy NowTrading
}


class FleetPositionGuardian:
    def __init__(self, target_gold_sl: float = 4170.00):
        self.target_gold_sl = target_gold_sl
        self.handled_tickets = set()
        self.initial_risk_map: Dict[int, float] = {}
        self.trailing_stage_map: Dict[int, int] = {}

    def _get_fallback_atr(self, mt5_mgr: MT5ConnectionManager, symbol: str) -> float:
        """Calculate 14-period H1 ATR as fallback if initial SL is missing."""
        try:
            df = mt5_mgr.get_rates(symbol, "H1", count=25)
            if df is not None and len(df) >= 15:
                tr = np.maximum(
                    df['high'] - df['low'],
                    np.maximum(
                        (df['high'] - df['close'].shift()).abs(),
                        (df['low'] - df['close'].shift()).abs()
                    )
                )
                atr = float(tr.rolling(14).mean().iloc[-1])
                if not math.isnan(atr) and atr > 0:
                    return atr
        except Exception as e:
            logger.warning(f"⚠️ Could not compute fallback ATR for {symbol}: {e}")
        return 0.0

    def calculate_position_r(self, pos: Dict[str, Any], initial_risk: float) -> float:
        """
        Calculate actual R-Multiple:
          Current_R = (Current_Price - Open_Price) / Initial_Risk (BUY)
          Current_R = (Open_Price - Current_Price) / Initial_Risk (SELL)
        """
        if initial_risk <= 0:
            return 0.0

        price_open = float(pos.get("price_open", 0.0))
        price_current = float(pos.get("price_current", price_open))
        pos_type = pos.get("type", "BUY")

        if pos_type in ("BUY", 0, mt5.ORDER_TYPE_BUY):
            return (price_current - price_open) / initial_risk
        else:
            return (price_open - price_current) / initial_risk

    def lock_all_open_positions_to_breakeven(self, mt5_mgr: MT5ConnectionManager) -> List[Dict[str, Any]]:
        """
        Enforce Target 2 zero-risk state: Move all open positions' SL to at least Entry +/- Spread.
        Called when Daily Profit Target 2 (+100$) is achieved.
        """
        if not mt5_mgr.ensure_connected():
            return []

        all_positions = mt5_mgr.get_positions()
        actions = []

        for p in all_positions:
            ticket = p["ticket"]
            symbol = p["symbol"]
            pos_type = p["type"]
            price_open = p["price_open"]
            sl = p["sl"]
            tp = p["tp"]

            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            point = spec.get("point", 0.01) if spec else 0.01
            spread = tick.get("spread", point * 10) if tick else (point * 10)
            slippage_buffer = 2 * point
            is_buy = pos_type in ("BUY", 0, mt5.ORDER_TYPE_BUY)

            if is_buy:
                target_sl = price_open + spread + slippage_buffer
                needs_update = (sl < target_sl)
            else:
                target_sl = price_open - (spread + slippage_buffer)
                needs_update = (sl > target_sl or sl == 0.0)

            if needs_update:
                res = mt5_mgr.modify_position_sltp(ticket=ticket, sl=target_sl, tp=tp)
                if res.get("success"):
                    applied_sl = res["sl"]
                    logger.info(f"🛡️ [GUARDIAN PROFIT LOCK] Position #{ticket} ({symbol}) secured to Breakeven SL={applied_sl}")
                    get_notifier().send_message(
                        f"🛡️ *[GUARDIAN]* Target 2 Hit -> Secured open position `#{ticket}` (`{symbol}`) to Breakeven SL (*{applied_sl}*)"
                    )
                    actions.append({"ticket": ticket, "status": "LOCKED_BREAKEVEN", "sl": applied_sl})

        return actions

    def inspect_and_protect(
        self,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: Optional[Any] = None
    ) -> List[Dict[str, Any]]:
        """
        Inspect all open positions on the account.
        Enforces two-stage trailing (1.5R breakeven, 2.0R profit lock),
        locks all positions to breakeven if Target 2 is hit,
        and protects orphan/unprotected trades.
        """
        if not mt5_mgr.ensure_connected():
            return []

        actions = []

        # If Daily Profit Target 2 (+100$) was triggered, enforce zero-risk on all positions
        if risk_guard is not None and getattr(risk_guard, "is_profit_locked", False):
            lock_actions = self.lock_all_open_positions_to_breakeven(mt5_mgr)
            actions.extend(lock_actions)

        all_positions = mt5_mgr.get_positions()

        # 1. Clean up closed tickets from internal memory maps
        active_tickets = {p["ticket"] for p in all_positions}
        for ticket in list(self.initial_risk_map.keys()):
            if ticket not in active_tickets:
                self.initial_risk_map.pop(ticket, None)
                self.trailing_stage_map.pop(ticket, None)

        # 2. Iterate each open position
        for p in all_positions:
            ticket = p.get("ticket", 0)
            magic = p.get("magic", 0)
            symbol = p.get("symbol", "")
            pos_type = p.get("type", "BUY")
            price_open = float(p.get("price_open", 0.0))
            price_current = float(p.get("price_current", price_open))
            profit = float(p.get("profit", 0.0))
            sl = float(p.get("sl", 0.0))
            tp = float(p.get("tp", 0.0))

            # Specific legacy rule for Golden Ticket #5223346770
            if ticket == 5223346770:
                if sl < self.target_gold_sl:
                    logger.warning(
                        f"🛡️ [GUARDIAN] Detected Unprotected Gold Position #{ticket} (Lãi: ${profit:+.2f}). "
                        f"Targeting SL={self.target_gold_sl} to lock profit..."
                    )
                    res = mt5_mgr.modify_position_sltp(ticket=ticket, sl=self.target_gold_sl, tp=tp)
                    if res.get("success"):
                        actions.append({"ticket": ticket, "status": "LOCKED_SL", "sl": self.target_gold_sl})
                continue

            # Determine / Register Initial Risk
            if ticket not in self.initial_risk_map:
                if sl > 0.0:
                    init_risk = abs(price_open - sl)
                else:
                    atr = self._get_fallback_atr(mt5_mgr, symbol)
                    init_risk = (1.5 * atr) if atr > 0 else (price_open * 0.005)
                # Bound check to avoid division by zero
                if init_risk <= 0:
                    init_risk = max(price_open * 0.005, 0.01)
                self.initial_risk_map[ticket] = init_risk
            else:
                init_risk = self.initial_risk_map[ticket]

            current_r = self.calculate_position_r(p, init_risk)
            current_stage = self.trailing_stage_map.get(ticket, 0)
            is_buy = pos_type in ("BUY", 0, mt5.ORDER_TYPE_BUY)

            spec = mt5_mgr.get_symbol_spec(symbol)
            tick = mt5_mgr.get_tick(symbol)
            point = spec.get("point", 0.01) if spec else 0.01
            digits = spec.get("digits", 2) if spec else 2
            spread = tick.get("spread", point * 10) if tick else (point * 10)
            slippage_buffer = 2 * point
            be_offset = spread + slippage_buffer

            # --- STAGE 2: PROFIT PROTECTION (>= +2.0R) ---
            if current_r >= 2.0:
                if is_buy:
                    target_sl = price_open + (0.75 * init_risk)
                    # Check H1 structure: trail previous H1 bar low if higher
                    h1_df = mt5_mgr.get_rates(symbol, "H1", count=5)
                    if h1_df is not None and len(h1_df) >= 2:
                        prev_low = float(h1_df['low'].iloc[-2])
                        target_sl = max(target_sl, prev_low)
                    needs_update = (sl == 0.0 or target_sl > sl)
                else:
                    target_sl = price_open - (0.75 * init_risk)
                    # Check H1 structure: trail previous H1 bar high if lower
                    h1_df = mt5_mgr.get_rates(symbol, "H1", count=5)
                    if h1_df is not None and len(h1_df) >= 2:
                        prev_high = float(h1_df['high'].iloc[-2])
                        target_sl = min(target_sl, prev_high)
                    needs_update = (sl == 0.0 or target_sl < sl)

                if needs_update:
                    res = mt5_mgr.modify_position_sltp(ticket=ticket, sl=target_sl, tp=tp)
                    if res.get("success"):
                        applied_sl = res["sl"]
                        self.trailing_stage_map[ticket] = 2
                        logger.info(
                            f"[GUARDIAN] Position #{ticket} reached {current_r:.2f}R -> Moving SL to {applied_sl} (Stage 2: Profit Protection)"
                        )
                        get_notifier().send_message(
                            f"🛡️ *[GUARDIAN]* Position `#{ticket}` (`{symbol}`) reached *{current_r:.2f}R*!\n"
                            f"👉 Trailed SL to *{applied_sl}* (Stage 2 Profit Lock)"
                        )
                        actions.append({"ticket": ticket, "status": "STAGE2_PROFIT_LOCK", "sl": applied_sl, "r": current_r})
                        continue

            # --- STAGE 1: BREAKEVEN LOCK (>= +1.5R) ---
            if current_r >= 1.5 and current_stage < 2:
                if is_buy:
                    target_be_sl = price_open + be_offset
                    needs_be = (sl < price_open or sl == 0.0)
                else:
                    target_be_sl = price_open - be_offset
                    needs_be = (sl > price_open or sl == 0.0)

                if needs_be:
                    res = mt5_mgr.modify_position_sltp(ticket=ticket, sl=target_be_sl, tp=tp)
                    if res.get("success"):
                        applied_sl = res["sl"]
                        self.trailing_stage_map[ticket] = 1
                        logger.info(
                            f"[GUARDIAN] Position #{ticket} reached {current_r:.2f}R -> Moving SL to {applied_sl} (Stage 1: Breakeven Lock)"
                        )
                        get_notifier().send_message(
                            f"🛡️ *[GUARDIAN]* Position `#{ticket}` (`{symbol}`) reached *{current_r:.2f}R*!\n"
                            f"👉 Moved SL to *{applied_sl}* (Stage 1 Breakeven Lock)"
                        )
                        actions.append({"ticket": ticket, "status": "STAGE1_BREAKEVEN", "sl": applied_sl, "r": current_r})
                        continue

            # General Orphan Check: Position not owned by known fleet bots
            if magic not in KNOWN_MAGICS and sl == 0.0:
                logger.warning(
                    f"⚠️ [GUARDIAN] Orphan position detected: Ticket #{ticket} ({symbol} {pos_type}) "
                    f"Magic={magic}, P/L=${profit:+.2f} has NO STOP LOSS!"
                )
                if spec and profit > 10.0:
                    be_sl = price_open
                    res = mt5_mgr.modify_position_sltp(ticket=ticket, sl=be_sl, tp=tp)
                    if res.get("success"):
                        logger.info(f"✅ [GUARDIAN] Applied Breakeven SL={be_sl} to orphan #{ticket}")
                        actions.append({"ticket": ticket, "status": "APPLIED_BREAKEVEN"})

        return actions
