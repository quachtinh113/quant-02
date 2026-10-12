"""
Unified Quant Fleet — Position Guardian & Orphan Position Protector
Monitors all open positions across the entire account.
Detects orphan positions (manual / test trades without SL) and enforces profit locking.
"""

from typing import Dict, Any, List
from datetime import datetime, timezone
import MetaTrader5 as mt5

from core.audit_logger import get_logger
from core.mt5_connection_manager import MT5ConnectionManager
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

    def inspect_and_protect(self, mt5_mgr: MT5ConnectionManager) -> List[Dict[str, Any]]:
        """
        Inspect all open positions on the account.
        Identify orphan or unprotected trades (SL == 0) and apply protection rules.
        """
        if not mt5_mgr.ensure_connected():
            return []

        all_positions = mt5_mgr.get_positions()
        actions = []

        for p in all_positions:
            ticket = p["ticket"]
            magic = p["magic"]
            symbol = p["symbol"]
            pos_type = p["type"]
            price_open = p["price_open"]
            price_current = p["price_current"]
            profit = p["profit"]
            sl = p["sl"]
            tp = p["tp"]

            # Specific rule for Golden Ticket #5223346770 (TEST_BUY_CONNECT)
            if ticket == 5223346770:
                if sl < self.target_gold_sl:
                    logger.warning(
                        f"🛡️ [GUARDIAN] Detected Unprotected Gold Position #{ticket} (Lãi: ${profit:+.2f}). "
                        f"Targeting SL={self.target_gold_sl} to lock +$60.00 profit..."
                    )
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": ticket,
                        "symbol": symbol,
                        "sl": self.target_gold_sl,
                        "tp": tp
                    }
                    res = mt5.order_send(req)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        logger.info(f"✅ [GUARDIAN] Successfully set SL={self.target_gold_sl} for #{ticket}!")
                        actions.append({"ticket": ticket, "status": "LOCKED_SL", "sl": self.target_gold_sl})
                    elif res and res.retcode == 10018:
                        logger.info(f"⏳ [GUARDIAN] Market closed. Will apply SL={self.target_gold_sl} immediately upon market open.")
                        actions.append({"ticket": ticket, "status": "PENDING_MARKET_OPEN", "target_sl": self.target_gold_sl})
                    else:
                        err = res.comment if res else str(mt5.last_error())
                        logger.error(f"❌ [GUARDIAN] Failed to set SL for #{ticket}: {err}")
                continue

            # General Orphan Check: Position not owned by known fleet bots
            if magic not in KNOWN_MAGICS:
                if sl == 0.0:
                    logger.warning(
                        f"⚠️ [GUARDIAN] Orphan position detected: Ticket #{ticket} ({symbol} {pos_type}) "
                        f"Magic={magic}, P/L=${profit:+.2f} has NO STOP LOSS!"
                    )
                    # If in healthy profit, try to move SL to breakeven
                    spec = mt5_mgr.get_symbol_spec(symbol)
                    if spec and profit > 10.0:
                        be_sl = price_open
                        req = {
                            "action": mt5.TRADE_ACTION_SLTP,
                            "position": ticket,
                            "symbol": symbol,
                            "sl": round(be_sl, spec["digits"]),
                            "tp": tp
                        }
                        res = mt5.order_send(req)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            logger.info(f"✅ [GUARDIAN] Applied Breakeven SL={be_sl} to orphan #{ticket}")
                            actions.append({"ticket": ticket, "status": "APPLIED_BREAKEVEN"})

        return actions
