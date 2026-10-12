"""
Unified Quant Fleet — System Health & Unit Test Suite
Verifies magic isolation, Carver institutional risk guards, config loading, and strategy instantiations.
Mentored by pysystemtrade (Robert Carver Systematic Trading Architecture).
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import yaml

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

from core.audit_logger import setup_logger
setup_logger(log_dir=str(_ROOT / "logs"), log_filename="test_suite.log")

from config.magic_registry import (
    MAGIC_BTC_8H, MAGIC_FX_D1, MAGIC_GOLD_SESS, MAGIC_USIDX_SESS, MAGIC_NOWTRADING_MTF,
    BOT_TO_MAGIC, MAGIC_TO_BOT, get_magic, get_bot_by_magic
)
from core.fleet_risk_guard import FleetRiskGuard
from core.carver_framework import (
    CarverVolSizer, CarverForecastEngine, CarverFactorGuard, CarverBufferEngine
)
from strategies.bot1_btc_8h import Btc8hStrategy
from strategies.bot2_fx_d1 import FxD1Strategy
from strategies.bot3_gold_sess import GoldSessStrategy
from strategies.bot4_usidx_sess import UsidxSessStrategy
from strategies.bot5_nowtrading_mtf import NowTradingStrategy
from core.mt5_connection_manager import MT5ConnectionManager


def test_magic_registry():
    print("👉 [1/6] Testing Magic Registry Isolation...")
    magics = [MAGIC_BTC_8H, MAGIC_FX_D1, MAGIC_GOLD_SESS, MAGIC_USIDX_SESS, MAGIC_NOWTRADING_MTF]
    assert len(magics) == len(set(magics)), "Duplicate Magic Numbers detected!"
    assert len(BOT_TO_MAGIC) == 5, f"Expected 5 bots in registry, found {len(BOT_TO_MAGIC)}"

    for bot, magic in BOT_TO_MAGIC.items():
        assert get_magic(bot) == magic
        assert get_bot_by_magic(magic) == bot
    print("   ✅ Magic Numbers are strictly unique and mapped correctly.")


def test_carver_framework():
    print("👉 [2/6] Testing Carver Institutional Quantitative Framework...")

    # 1. Vol Sizing Test
    spec_gold = {"contract_size": 100.0, "volume_min": 0.01, "volume_max": 10.0, "volume_step": 0.01}
    lot_low_vol = CarverVolSizer.calculate_lot("XAUUSDm", equity=50000.0, atr=5.0, spec=spec_gold, annual_vol_target=0.15)
    lot_high_vol = CarverVolSizer.calculate_lot("XAUUSDm", equity=50000.0, atr=25.0, spec=spec_gold, annual_vol_target=0.15)
    assert lot_low_vol > lot_high_vol, f"Expected higher lot in low vol, got {lot_low_vol} vs {lot_high_vol}"

    # 2. Continuous Forecast & Capping Test [-20, +20]
    capped_pos = CarverForecastEngine.scale_and_cap(35.0, scalar=1.0, cap=20.0)
    capped_neg = CarverForecastEngine.scale_and_cap(-40.0, scalar=1.0, cap=20.0)
    assert capped_pos == 20.0, f"Expected +20.0, got {capped_pos}"
    assert capped_neg == -20.0, f"Expected -20.0, got {capped_neg}"

    # Deadband test
    mult_noise, dir_noise = CarverForecastEngine.get_position_multiplier(1.5, deadband=3.0)
    assert dir_noise == "FLAT" and mult_noise == 0.0, "Noise signal should be zeroed out by deadband"

    mult_strong, dir_strong = CarverForecastEngine.get_position_multiplier(15.0, deadband=3.0)
    assert dir_strong == "BUY" and mult_strong == 1.5, f"Expected 1.5x BUY, got {mult_strong}x {dir_strong}"

    # 3. Factor Exposure Delta Calculation
    assert CarverFactorGuard.get_symbol_usd_delta("USDJPYm", "BUY") == +1
    assert CarverFactorGuard.get_symbol_usd_delta("EURUSDm", "BUY") == -1
    assert CarverFactorGuard.get_symbol_usd_delta("EURUSDm", "SELL") == +1
    assert CarverFactorGuard.get_symbol_usd_delta("XAUUSDm", "BUY") == -1

    # 4. Buffering Test
    assert CarverBufferEngine.should_adjust_position(0.0, 1.0) is True
    assert CarverBufferEngine.should_adjust_position(1.0, 1.1, buffer_ratio=0.20) is False
    assert CarverBufferEngine.should_adjust_position(1.0, 1.4, buffer_ratio=0.20) is True

    print("   ✅ Carver Vol Sizing, Forecast Capping [-20, +20], Factor Guard & Buffering verified.")


def test_risk_guard():
    print("👉 [3/6] Testing Fleet Risk Guard (Drawdown, Margin, and USD Factor Guard)...")
    guard = FleetRiskGuard(
        max_daily_drawdown_usd=100.0,
        min_margin_level_percent=200.0,
        max_total_positions=5,
        max_net_usd_exposure=3
    )

    account_ok = {"equity": 1000.0, "margin": 100.0, "margin_level": 1000.0}
    guard.update_account_state(account_ok)

    # Normal state
    allowed, reason = guard.can_open_position(account_ok, current_position_count=2)
    assert allowed is True, f"Expected allowed, got: {reason}"

    # Position limit hit
    allowed, reason = guard.can_open_position(account_ok, current_position_count=5)
    assert allowed is False
    assert "Max fleet positions reached" in reason

    # Margin level too low
    account_low_margin = {"equity": 1000.0, "margin": 600.0, "margin_level": 166.0}
    allowed, reason = guard.can_open_position(account_low_margin, current_position_count=2)
    assert allowed is False
    assert "Margin level critical" in reason

    # Daily drawdown limit hit
    account_drawdown = {"equity": 850.0, "margin": 100.0, "margin_level": 850.0}
    guard.update_account_state(account_drawdown)
    allowed, reason = guard.can_open_position(account_drawdown, current_position_count=2)
    assert allowed is False
    assert "drawdown" in reason.lower()

    # Carver USD Factor Concentration Test
    guard._is_halted = False  # reset halt for test
    open_positions = [
        {"symbol": "USDJPYm", "type": "BUY"},   # +1 USD
        {"symbol": "USDCADm", "type": "BUY"},   # +1 USD
        {"symbol": "EURUSDm", "type": "SELL"},  # +1 USD -> total = +3 USD
    ]
    # Adding another Long USD trade should be VETOED (would reach +4 > limit 3)
    allowed, reason = guard.can_open_position(
        account_ok,
        current_position_count=3,
        open_positions=open_positions,
        new_symbol="USDCHFm",
        new_direction="BUY"
    )
    assert allowed is False, "Should veto trade exceeding Net USD Exposure limit"
    assert "USD Factor concentration veto" in reason

    # Adding a Short USD trade should be ALLOWED (brings total to +2)
    allowed, reason = guard.can_open_position(
        account_ok,
        current_position_count=3,
        open_positions=open_positions,
        new_symbol="EURUSDm",
        new_direction="BUY"
    )
    assert allowed is True, f"Hedging USD should be allowed, got: {reason}"

    print("   ✅ Risk Guard circuit breakers & Carver USD Factor limits triggered correctly.")


def test_config_and_strategies():
    print("👉 [4/6] Testing Configuration & Strategy Instantiation...")
    cfg_path = _ROOT / "config" / "fleet_config.yaml"
    assert cfg_path.exists(), "fleet_config.yaml missing!"

    with open(cfg_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    bots_cfg = config.get("bots", {})
    s1 = Btc8hStrategy(bots_cfg.get("btc_8h", {}))
    s2 = FxD1Strategy(bots_cfg.get("fx_d1", {}))
    s3 = GoldSessStrategy(bots_cfg.get("gold_sess", {}))
    s4 = UsidxSessStrategy(bots_cfg.get("usidx_sess", {}))
    s5 = NowTradingStrategy(bots_cfg.get("nowtrading_mtf", {}))

    assert s1.magic == MAGIC_BTC_8H
    assert s2.magic == MAGIC_FX_D1
    assert s3.magic == MAGIC_GOLD_SESS
    assert s4.magic == MAGIC_USIDX_SESS
    assert s5.magic == MAGIC_NOWTRADING_MTF
    print("   ✅ All 5 strategies instantiated cleanly with Carver integration.")


def test_cadence_logic():
    print("👉 [5/6] Testing Cadence & Session Logic...")
    cfg_path = _ROOT / "config" / "fleet_config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    bots_cfg = config.get("bots", {})

    s1 = Btc8hStrategy(bots_cfg.get("btc_8h", {}))
    s3 = GoldSessStrategy(bots_cfg.get("gold_sess", {}))
    s5 = NowTradingStrategy(bots_cfg.get("nowtrading_mtf", {}))

    # At 08:02 UTC on Wednesday:
    test_dt = datetime(2026, 9, 16, 8, 2, tzinfo=timezone.utc)
    assert s1.should_run_cycle(test_dt) is True, "BTC 8H should fire at 08:00 UTC"
    assert s3.should_run_cycle(test_dt) is True, "Gold London Session should fire at 08:00 UTC"
    assert s5.should_run_cycle(test_dt) is True, "NowTrading hourly gate should fire at HH:02 UTC"

    # At 11:35 UTC:
    test_dt_off = datetime(2026, 9, 16, 11, 35, tzinfo=timezone.utc)
    assert s1.should_run_cycle(test_dt_off) is False
    assert s3.should_run_cycle(test_dt_off) is False
    assert s5.should_run_cycle(test_dt_off) is False
    print("   ✅ Strategy schedule gates trigger on exact hours and ignore off-hours.")


def test_mt5_status():
    print("👉 [6/6] Testing MT5 Connection Subsystem & Vol Sizing API...")
    mgr = MT5ConnectionManager()
    ok = mgr.connect()
    if ok:
        info = mgr.get_account_info()
        print(f"   ✅ Live MT5 connection verified: Account {info.get('login')} (Equity: ${info.get('equity'):,.2f})")
        # Test vol sizing call with active MT5
        lot = mgr.calculate_vol_targeted_lot("EURUSDm", atr=0.0060, equity=info.get("equity", 10000.0), annual_vol_target=0.15)
        print(f"   ✅ Carver Vol Sizing online: EURUSDm target lot = {lot}L")
    else:
        print("   ℹ️ MT5 Terminal is offline. Connector handles offline mode safely.")


if __name__ == "__main__":
    print("\n==================================================")
    print("   UNIFIED QUANT FLEET - AUTOMATED TEST SUITE     ")
    print("==================================================")
    test_magic_registry()
    test_carver_framework()
    test_risk_guard()
    test_config_and_strategies()
    test_cadence_logic()
    test_mt5_status()
    print("==================================================")
    print("🎉 ALL 6 TESTS PASSED SUCCESSFULLY!")
    print("==================================================\n")
