"""
Unified Quant Fleet — Magic Number Registry
Single Source of Truth for order isolation across all 5 bot strategies.
Prevents order collision when running multiple strategies on a single MT5 account.
"""

from typing import Dict, Optional

# --- Magic Number Allocation Table ---
MAGIC_FX_D1: int = 260901         # Bot 2: Forex Daily Close
MAGIC_USIDX_SESS: int = 260902    # Bot 4: US Indices Session (US500, USTEC)
MAGIC_GOLD_SESS: int = 260903     # Bot 3: Gold Session (XAUUSD, XAGUSD)
MAGIC_BTC_8H: int = 260904        # Bot 1: Bitcoin 8-Hour (BTCUSD)
MAGIC_NOWTRADING_MTF: int = 260905 # Bot 5: NowTrading 2.1 Multi-Timeframe (RSI + ADX + ATR DCA)

# Legacy compatibility (e.g., positions opened with old NowTrading default)
MAGIC_LEGACY_NOWTRADING: int = 202500

BOT_TO_MAGIC: Dict[str, int] = {
    "btc_8h": MAGIC_BTC_8H,
    "fx_d1": MAGIC_FX_D1,
    "gold_sess": MAGIC_GOLD_SESS,
    "usidx_sess": MAGIC_USIDX_SESS,
    "nowtrading_mtf": MAGIC_NOWTRADING_MTF,
}

MAGIC_TO_BOT: Dict[int, str] = {
    MAGIC_BTC_8H: "btc_8h",
    MAGIC_FX_D1: "fx_d1",
    MAGIC_GOLD_SESS: "gold_sess",
    MAGIC_USIDX_SESS: "usidx_sess",
    MAGIC_NOWTRADING_MTF: "nowtrading_mtf",
    MAGIC_LEGACY_NOWTRADING: "nowtrading_mtf",  # legacy mapped to nowtrading
}

def get_magic(bot_name: str) -> int:
    """Return the assigned magic number for a bot strategy."""
    if bot_name not in BOT_TO_MAGIC:
        raise KeyError(f"Unknown bot name '{bot_name}'. Registered: {list(BOT_TO_MAGIC.keys())}")
    return BOT_TO_MAGIC[bot_name]

def get_bot_by_magic(magic: int) -> Optional[str]:
    """Return the bot name for a given magic number, or None if external/manual."""
    return MAGIC_TO_BOT.get(magic)
