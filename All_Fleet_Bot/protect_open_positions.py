"""
Unified Quant Fleet — Quick Position Guardian Runner
Safeguards open positions, checks orphan trades, and prepares SL locking.
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_guardian import FleetPositionGuardian

def main():
    print("==========================================================================")
    print("🛡️  UNIFIED QUANT FLEET — POSITION GUARDIAN")
    print("==========================================================================")
    
    mt5_mgr = MT5ConnectionManager()
    if not mt5_mgr.connect():
        print("❌ Cannot connect to MT5. Please check terminal path & credentials.")
        return

    guardian = FleetPositionGuardian(target_gold_sl=4170.00)
    
    positions = mt5_mgr.get_positions()
    print(f"📊 Total open positions found: {len(positions)}")
    for p in positions:
        print(f"   • Ticket #{p['ticket']} | {p['symbol']:<8} | {p['type']:<4} {p['volume']}L | Open: {p['price_open']} | Current: {p['price_current']} | SL: {p['sl']} | TP: {p['tp']} | Floating: ${p['profit']:+.2f} | Magic: {p['magic']}")

    print("\n🔍 Running Guardian audit & protection...")
    actions = guardian.inspect_and_protect(mt5_mgr)
    
    for a in actions:
        print(f"   👉 Action on Ticket #{a.get('ticket')}: {a.get('status')}")

    print("\n✅ Position inspection complete!")
    print("==========================================================================")

if __name__ == "__main__":
    main()
