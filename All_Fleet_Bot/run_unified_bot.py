"""
=====================================================================
UNIFIED QUANT FLEET — MASTER ORCHESTRATOR
1 PC running 5 Quant Bot Strategies simultaneously on MetaTrader 5:
  1. exness_btc_8h       (Magic: 260904) - Bitcoin 8-Hour
  2. exness_fx_d1        (Magic: 260901) - FX Majors Daily Close
  3. exness_gold_sess    (Magic: 260903) - Gold & Silver Session (London/NY)
  4. exness_usidx_sess   (Magic: 260902) - US Indices Session (US500/USTEC)
  5. nowtrading_mtf      (Magic: 260905) - NowTrading 2.1 Multi-Timeframe DCA
=====================================================================
"""

import os
import sys
import time
import signal
import threading
from pathlib import Path
from datetime import datetime, timezone
import yaml
from dotenv import load_dotenv

# Ensure root is in sys.path
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from core.audit_logger import setup_logger, get_logger
from core.mt5_connection_manager import MT5ConnectionManager
from core.fleet_risk_guard import FleetRiskGuard
from core.fleet_guardian import FleetPositionGuardian
from strategies.bot1_btc_8h import Btc8hStrategy
from strategies.bot2_fx_d1 import FxD1Strategy
from strategies.bot3_gold_sess import GoldSessStrategy
from strategies.bot4_usidx_sess import UsidxSessStrategy
from strategies.bot5_nowtrading_mtf import NowTradingStrategy

# Load environment & config
load_dotenv(_ROOT / ".env")
CONFIG_PATH = _ROOT / "config" / "fleet_config.yaml"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

system_cfg = config.get("system", {})
risk_cfg = config.get("account_risk_guard", {})
bots_cfg = config.get("bots", {})

logger = setup_logger(
    log_dir=str(_ROOT / system_cfg.get("log_dir", "logs")),
    log_level=system_cfg.get("log_level", "INFO")
)

# Graceful termination
_running = True


def handle_shutdown(signum, frame):
    global _running
    logger.info("🛑 Shutdown signal received. Stopping Unified Quant Fleet safely...")
    _running = False


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


class Watchdog(threading.Thread):
    """Monitors the main thread and alerts if stalled."""
    def __init__(self, timeout_seconds=180):
        super().__init__(daemon=True)
        self.timeout = timeout_seconds
        self.last_ping = time.time()
        self.running = True

    def ping(self):
        self.last_ping = time.time()

    def run(self):
        last_time = time.time()
        while self.running and _running:
            time.sleep(10)
            now = time.time()
            elapsed = now - last_time
            last_time = now

            # Detect sleep/hibernation
            if elapsed > 30.0:
                logger.info(f"⏰ System wake from sleep detected ({elapsed:.1f}s sleep). Resetting watchdog timer.")
                self.ping()
                continue

            if now - self.last_ping > self.timeout:
                logger.critical(f"💀 WATCHDOG ALERT: Main loop stalled for > {self.timeout}s! Reconnecting...")
                self.ping()


def print_fleet_dashboard(account_info, risk_status, bot_summaries):
    """Print an aesthetic ANSI dashboard to the console."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "",
        "==========================================================================================",
        f" 🛡️  UNIFIED QUANT FLEET DASHBOARD — 5 BOTS ON 1 PC | {now_utc}",
        "==========================================================================================",
        f" Account: {account_info.get('login')} @ {account_info.get('server')} | "
        f"Balance: ${account_info.get('balance', 0):,.2f} | Equity: ${account_info.get('equity', 0):,.2f} | "
        f"Margin Level: {account_info.get('margin_level', 0):.1f}%",
        f" Daily Peak: ${risk_status.get('daily_peak_equity', 0):,.2f} | "
        f"Current DD: ${risk_status.get('current_drawdown_usd', 0):.2f} / Max ${risk_status.get('drawdown_limit_usd', 0):.2f} | "
        f"Net USD Factor: {risk_status.get('net_usd_exposure', 0):+d} (Max +/-{risk_status.get('max_net_usd_exposure', 4)}) | "
        f"Status: {'🔴 HALTED' if risk_status.get('is_halted') else '🟢 ACTIVE'}",
        "------------------------------------------------------------------------------------------",
        f" {'Bot Name':<16} | {'Magic':<7} | {'Status':<8} | {'Open Pos':<8} | {'Unrealized P/L':<14} | {'Last Action'}",
        "------------------------------------------------------------------------------------------",
    ]

    total_positions = 0
    total_unrealized = 0.0

    for b in bot_summaries:
        status_str = "ENABLED" if b["enabled"] else "DISABLED"
        pnl_str = f"${b['unrealized_pnl']:+.2f}"
        total_positions += b["positions_count"]
        total_unrealized += b["unrealized_pnl"]
        lines.append(
            f" {b['name']:<16} | {b['magic']:<7} | {status_str:<8} | {b['positions_count']:<8} | {pnl_str:<14} | {b['last_action']}"
        )

    lines.append("------------------------------------------------------------------------------------------")
    lines.append(f" TOTAL OPEN POSITIONS: {total_positions} | TOTAL UNREALIZED P/L: ${total_unrealized:+.2f}")
    lines.append("==========================================================================================")
    lines.append("")

    print("\n".join(lines), flush=True)


def main():
    logger.info("=================================================================")
    logger.info("🚀 INITIALIZING UNIFIED QUANT FLEET (5 BOTS ON 1 PC)...")
    logger.info("=================================================================")

    # 1. Connect MT5
    mt5_mgr = MT5ConnectionManager()
    if not mt5_mgr.connect():
        logger.critical("❌ Failed initial MT5 connection. Please ensure MT5 terminal is open/accessible.")
        # We still proceed into the loop to let auto-reconnect retry

    # 2. Risk Guard (with Carver Factor Exposure Guard)
    risk_guard = FleetRiskGuard(
        max_daily_drawdown_usd=float(risk_cfg.get("max_daily_drawdown_usd", 300.0)),
        min_margin_level_percent=float(risk_cfg.get("min_margin_level_percent", 200.0)),
        max_total_positions=int(risk_cfg.get("max_total_positions", 10)),
        max_net_usd_exposure=int(risk_cfg.get("max_net_usd_exposure", 4)),
        halt_file=str(_ROOT / "HALT_FLEET")
    )

    # 3. Instantiate the 5 Strategies
    strategies = [
        Btc8hStrategy(bots_cfg.get("btc_8h", {})),
        FxD1Strategy(bots_cfg.get("fx_d1", {})),
        GoldSessStrategy(bots_cfg.get("gold_sess", {})),
        UsidxSessStrategy(bots_cfg.get("usidx_sess", {})),
        NowTradingStrategy(bots_cfg.get("nowtrading_mtf", {})),
    ]

    logger.info(f"✨ Successfully loaded {len(strategies)} strategy engines:")
    for s in strategies:
        logger.info(f"   - {s.name:<16} (Magic: {s.magic}, Enabled: {s.enabled})")

    # 4. Watchdog & Position Guardian
    guardian = FleetPositionGuardian(target_gold_sl=4170.00)
    watchdog = Watchdog(timeout_seconds=180)
    watchdog.start()

    loop_interval = system_cfg.get("loop_interval_seconds", 15)
    last_dashboard_time = 0.0

    logger.info(f"🔄 Starting Master Execution Loop (interval = {loop_interval}s)...")

    # 5. Master Execution Loop
    while _running:
        try:
            watchdog.ping()
            now_utc = datetime.now(timezone.utc)

            # Ensure MT5 is online
            if not mt5_mgr.ensure_connected():
                logger.warning("⏳ Waiting for MT5 connection...")
                time.sleep(5)
                continue

            account_info = mt5_mgr.get_account_info()
            if not account_info:
                time.sleep(5)
                continue

            # Update Risk Engine
            open_positions = mt5_mgr.get_positions()
            risk_guard.update_account_state(account_info)
            risk_status = risk_guard.get_status(account_info, open_positions=open_positions)

            # Audit and protect orphan/unprotected positions
            guardian.inspect_and_protect(mt5_mgr)

            # Evaluate each bot
            for strat in strategies:
                if not strat.enabled:
                    continue

                # 1. Real-time tick hook (Take profit, time decay, basket checks)
                strat.on_tick_update(now_utc, mt5_mgr, risk_guard, account_info)

                # 2. Scheduled cycle check (Bar close / session window)
                if strat.should_run_cycle(now_utc):
                    strat.evaluate_cycle(now_utc, mt5_mgr, risk_guard, account_info)
                else:
                    # Sync open positions periodically
                    strat.sync_positions(mt5_mgr)

            # Dashboard refresh every 60 seconds
            if time.time() - last_dashboard_time >= 60.0:
                last_dashboard_time = time.time()
                bot_summaries = [s.get_summary() for s in strategies]
                print_fleet_dashboard(account_info, risk_status, bot_summaries)

            time.sleep(loop_interval)

        except Exception as e:
            logger.error(f"⚠️ Unhandled error in Master Loop: {e}", exc_info=True)
            time.sleep(5)

    # Clean shutdown
    logger.info("🔌 Shutting down Unified Quant Fleet...")
    watchdog.running = False
    mt5_mgr.shutdown()
    logger.info("👋 Unified Quant Fleet stopped cleanly. Goodbye!")


if __name__ == "__main__":
    main()
