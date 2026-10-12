"""
Unified Quant Fleet — Base Strategy Class
Common contract and lifecycle hooks for all 5 quant strategies.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List
from datetime import datetime

from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.audit_logger import get_logger

logger = get_logger()


class BaseStrategy(ABC):
    def __init__(self, name: str, magic: int, config: Dict[str, Any]):
        self.name = name
        self.magic = magic
        self.config = config
        self.enabled = config.get("enabled", True)
        self.last_decision_time = None
        self.last_action = "INITIALIZED"
        self.open_positions: List[Dict[str, Any]] = []

    def sync_positions(self, mt5_mgr: MT5ConnectionManager) -> List[Dict[str, Any]]:
        """Sync open positions belonging to this strategy's magic number."""
        self.open_positions = mt5_mgr.get_positions(magic=self.magic)
        return self.open_positions

    @abstractmethod
    def should_run_cycle(self, now_utc: datetime) -> bool:
        """Evaluate if the strategy's decision cadence is met."""
        pass

    @abstractmethod
    def evaluate_cycle(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        """Run strategy decision cycle (signal generation, entry, position management)."""
        pass

    def on_tick_update(
        self,
        now_utc: datetime,
        mt5_mgr: MT5ConnectionManager,
        risk_guard: FleetRiskGuard,
        account_info: Dict[str, Any]
    ) -> None:
        """Optional hook called on every master loop tick for real-time trailing/profit exits."""
        pass

    def get_summary(self) -> Dict[str, Any]:
        """Summary for the live fleet dashboard."""
        total_pnl = sum(p.get("profit", 0.0) for p in self.open_positions)
        return {
            "name": self.name,
            "magic": self.magic,
            "enabled": self.enabled,
            "positions_count": len(self.open_positions),
            "unrealized_pnl": total_pnl,
            "last_action": self.last_action,
            "last_decision_time": self.last_decision_time.strftime("%H:%M:%S") if self.last_decision_time else "Never",
        }
