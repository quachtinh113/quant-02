"""
Unified Quant Fleet — Dynamic Slippage & Execution Audit Logger
Institutional-grade execution quality analytics for MetaTrader 5:
  1. Records requested price, executed price, slippage (points/pips), and latency (ms).
  2. Persists audit trails in both high-speed SQLite database and append-only JSONL.
  3. Detects execution degradation: Triggers alerts if rolling slippage > 1.5 * spread.
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path

from core.audit_logger import get_logger
from core.telegram_notifier import get_notifier

logger = get_logger()


class ExecutionAuditLogger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ExecutionAuditLogger, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path: Optional[str] = None, jsonl_path: Optional[str] = None):
        if self._initialized:
            return

        base_dir = Path(__file__).resolve().parent.parent / "logs"
        base_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = str(db_path or (base_dir / "execution_audit.db"))
        self.jsonl_path = str(jsonl_path or (base_dir / "execution_audit.jsonl"))

        self._recent_executions: Dict[str, List[Dict[str, Any]]] = {}
        self._init_sqlite()
        self._initialized = True
        logger.info(f"📊 [EXECUTION AUDIT] Initialized. DB: {os.path.basename(self.db_path)}, JSONL: {os.path.basename(self.jsonl_path)}")

    def _init_sqlite(self) -> None:
        """Create execution audit table in SQLite if not exists."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    order_ticket INTEGER,
                    deal_ticket INTEGER,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    volume REAL NOT NULL,
                    magic INTEGER NOT NULL,
                    requested_price REAL NOT NULL,
                    executed_price REAL NOT NULL,
                    slippage_points REAL NOT NULL,
                    slippage_pips REAL NOT NULL,
                    latency_ms REAL NOT NULL,
                    spread_points REAL NOT NULL,
                    comment TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec_symbol ON order_executions(symbol)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec_timestamp ON order_executions(timestamp)")
            conn.commit()
        finally:
            conn.close()

    def record_execution(
        self,
        order_ticket: int,
        deal_ticket: int,
        symbol: str,
        direction: str,
        volume: float,
        magic: int,
        requested_price: float,
        executed_price: float,
        point: float,
        digits: int,
        latency_ms: float,
        spread_points: float = 0.0,
        comment: str = ""
    ) -> Dict[str, Any]:
        """
        Record and audit an executed order:
          - Calculates slippage in points and pips.
          - Positive slippage = worse fill (adverse slippage).
          - Negative slippage = price improvement (favorable slippage).
        """
        if point <= 0:
            point = 0.0001

        # Pip scale: 1 pip = 10 points for 3/5 digit symbols, else 1 point
        pip_ratio = 10.0 if digits in (3, 5) else 1.0

        is_buy = str(direction).upper() == "BUY"
        if is_buy:
            slippage_points = (executed_price - requested_price) / point
        else:
            slippage_points = (requested_price - executed_price) / point

        slippage_pips = slippage_points / pip_ratio
        now_iso = datetime.now(timezone.utc).isoformat()

        record = {
            "timestamp": now_iso,
            "order_ticket": int(order_ticket),
            "deal_ticket": int(deal_ticket),
            "symbol": str(symbol),
            "direction": str(direction).upper(),
            "volume": float(volume),
            "magic": int(magic),
            "requested_price": float(requested_price),
            "executed_price": float(executed_price),
            "slippage_points": round(slippage_points, 2),
            "slippage_pips": round(slippage_pips, 2),
            "latency_ms": round(latency_ms, 2),
            "spread_points": round(spread_points, 2),
            "comment": str(comment)
        }

        # 1. Write to JSONL
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            logger.error(f"❌ [EXECUTION AUDIT] Failed writing JSONL: {e}")

        # 2. Write to SQLite
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO order_executions (
                    timestamp, order_ticket, deal_ticket, symbol, direction,
                    volume, magic, requested_price, executed_price,
                    slippage_points, slippage_pips, latency_ms, spread_points, comment
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record["timestamp"], record["order_ticket"], record["deal_ticket"],
                record["symbol"], record["direction"], record["volume"], record["magic"],
                record["requested_price"], record["executed_price"], record["slippage_points"],
                record["slippage_pips"], record["latency_ms"], record["spread_points"], record["comment"]
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"❌ [EXECUTION AUDIT] Failed writing SQLite: {e}")
        finally:
            if conn:
                conn.close()

        # 3. Update memory cache and check for execution degradation alert
        clean_sym = symbol.rstrip("m").upper()
        if clean_sym not in self._recent_executions:
            self._recent_executions[clean_sym] = []
        self._recent_executions[clean_sym].append(record)
        if len(self._recent_executions[clean_sym]) > 20:
            self._recent_executions[clean_sym].pop(0)

        self._check_slippage_degradation(clean_sym, spread_points)

        logger.info(
            f"⚡ [EXECUTION AUDIT] #{order_ticket} | {symbol} {direction} {volume}L | "
            f"Req: {requested_price:.5f} -> Exec: {executed_price:.5f} | "
            f"Slippage: {slippage_pips:+.1f} pips ({slippage_points:+.1f} pts) | Latency: {latency_ms:.1f}ms"
        )
        return record

    def _check_slippage_degradation(self, clean_sym: str, current_spread_points: float) -> None:
        """
        Check if rolling average slippage over the last 5 trades exceeds 1.5 * spread.
        Triggers telemetry alert if slippage is abnormally high.
        """
        recent = self._recent_executions.get(clean_sym, [])
        if len(recent) < 3:
            return

        last_samples = recent[-5:]
        avg_slippage = sum(r["slippage_points"] for r in last_samples) / len(last_samples)
        benchmark_spread = max(current_spread_points, 10.0)

        if avg_slippage > (1.5 * benchmark_spread):
            alert_msg = (
                f"🚨 *[EXECUTION WARNING] EXCESSIVE SLIPPAGE DETECTED*\n"
                f"-----------------------------------------\n"
                f"• *Symbol:* `{clean_sym}`\n"
                f"• *Rolling Avg Slippage:* `{avg_slippage:+.1f} points`\n"
                f"• *Current Spread:* `{benchmark_spread:.1f} points`\n"
                f"• *Threshold:* `>{1.5 * benchmark_spread:.1f} points (1.5x Spread)`\n"
                f"• *Sample Count:* Last `{len(last_samples)} deals`\n"
                f"• *Action:* Inspect broker liquidity & order routing execution.\n"
                f"-----------------------------------------"
            )
            logger.warning(f"🚨 [SLIPPAGE DEGRADATION] {clean_sym} avg slippage {avg_slippage:.1f} pts > 1.5x spread {benchmark_spread:.1f} pts")
            try:
                get_notifier().send_message(alert_msg)
            except Exception:
                pass

    def get_summary_statistics(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Query aggregate slippage and latency metrics from SQLite."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            query = """
                SELECT 
                    COUNT(*),
                    AVG(slippage_pips),
                    MAX(slippage_pips),
                    MIN(slippage_pips),
                    AVG(latency_ms),
                    MAX(latency_ms)
                FROM order_executions
            """
            params = ()
            if symbol:
                query += " WHERE symbol = ?"
                params = (symbol,)

            cursor.execute(query, params)
            row = cursor.fetchone()
            if row and row[0] > 0:
                return {
                    "total_trades": row[0],
                    "avg_slippage_pips": round(row[1] or 0.0, 2),
                    "max_slippage_pips": round(row[2] or 0.0, 2),
                    "min_slippage_pips": round(row[3] or 0.0, 2),
                    "avg_latency_ms": round(row[4] or 0.0, 2),
                    "max_latency_ms": round(row[5] or 0.0, 2),
                }
        except Exception as e:
            logger.error(f"❌ [EXECUTION AUDIT] Error computing stats: {e}")
        finally:
            if conn:
                conn.close()

        return {
            "total_trades": 0,
            "avg_slippage_pips": 0.0,
            "max_slippage_pips": 0.0,
            "min_slippage_pips": 0.0,
            "avg_latency_ms": 0.0,
            "max_latency_ms": 0.0,
        }


def get_execution_auditor() -> ExecutionAuditLogger:
    """Singleton factory for ExecutionAuditLogger."""
    return ExecutionAuditLogger()
