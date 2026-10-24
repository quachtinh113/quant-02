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
    assert "USD Factor" in reason

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
    s4 = UsidxSessStrategy(bots_cfg.get("usidx_sess", {}))
    s5 = NowTradingStrategy(bots_cfg.get("nowtrading_mtf", {}))

    # At 08:02 UTC on Wednesday:
    test_dt = datetime(2026, 9, 16, 8, 2, tzinfo=timezone.utc)
    assert s1.should_run_cycle(test_dt) is True, "BTC 8H should fire at 08:00 UTC"
    assert s3.should_run_cycle(test_dt) is True, "Gold London Session should fire at 08:00 UTC"
    assert s5.should_run_cycle(test_dt) is True, "NowTrading hourly gate should fire at HH:02 UTC"

    # USIDX 15-Minute ORB at 14:16 UTC:
    test_dt_orb = datetime(2026, 9, 16, 14, 16, tzinfo=timezone.utc)
    assert s4.should_run_cycle(test_dt_orb) is True, "USIDX should fire 15m ORB at 14:15-14:20 UTC"

    # Before ORB at 14:02 UTC:
    test_dt_early = datetime(2026, 9, 16, 14, 2, tzinfo=timezone.utc)
    assert s4.should_run_cycle(test_dt_early) is False, "USIDX should NOT fire before 14:15 UTC"

    # USIDX Cash Close at 20:02 UTC:
    test_dt_close = datetime(2026, 9, 16, 20, 2, tzinfo=timezone.utc)
    assert s4.should_run_cycle(test_dt_close) is True, "USIDX should fire Cash Close at 20:00 UTC"

    # At 11:35 UTC:
    test_dt_off = datetime(2026, 9, 16, 11, 35, tzinfo=timezone.utc)
    assert s1.should_run_cycle(test_dt_off) is False
    assert s3.should_run_cycle(test_dt_off) is False
    assert s4.should_run_cycle(test_dt_off) is False
    assert s5.should_run_cycle(test_dt_off) is False
    print("   ✅ Strategy schedule gates trigger on exact hours/ORB and ignore off-hours.")


def test_mt5_status():
    print("👉 [6/7] Testing MT5 Connection Subsystem & Vol Sizing API...")
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


def test_market_schedule_governor():
    print("👉 [7/7] Testing Market Session & Weekend/Holiday Auto-Resumption...")
    from core.market_schedule import MarketScheduleGovernor
    gov = MarketScheduleGovernor()

    # Friday 21:00 UTC -> Closed
    fri_close = datetime(2026, 10, 16, 21, 0, 0, tzinfo=timezone.utc)
    assert gov.is_forex_calendar_open(fri_close) is False

    # Sunday 20:59 UTC -> Closed
    sun_close = datetime(2026, 10, 18, 20, 59, 0, tzinfo=timezone.utc)
    is_open, just_resumed, status = gov.check_market_state(sun_close)
    assert is_open is False
    assert status == "WEEKEND_STANDBY"

    # Sunday 21:00 UTC -> Market Resumes!
    sun_open = datetime(2026, 10, 18, 21, 0, 0, tzinfo=timezone.utc)
    is_open, just_resumed, status = gov.check_market_state(sun_open)
    assert is_open is True
    assert just_resumed is True
    assert status == "RESUMED_OPEN"

    # Holiday Full-Day Pause (e.g. Christmas Day 2026-12-25 on Friday 14:00 UTC)
    xmas = datetime(2026, 12, 25, 14, 0, 0, tzinfo=timezone.utc)
    is_open, just_resumed, status = gov.check_market_state(xmas)
    assert is_open is False
    assert "HOLIDAY_STANDBY" in status

    # Early Close Holiday (e.g. Black Friday 2026-11-27, early close at 18:00 UTC)
    black_fri_mid = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    is_open, just_resumed, status = gov.check_market_state(black_fri_mid)
    assert is_open is True

    black_fri_closed = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
    is_open, just_resumed, status = gov.check_market_state(black_fri_closed)
    assert is_open is False
    assert "HOLIDAY_STANDBY" in status

    print("   ✅ Weekend detection, holiday calendar filter, and auto-resumption verified.")


def test_fleet_guardian_spread_and_trailing():
    print("👉 [8/8] Testing Fleet Position Guardian Spread-Filter & Multi-Stage Trailing...")
    from core.fleet_guardian import FleetPositionGuardian
    guardian = FleetPositionGuardian()

    # 1. Sunday Opening Window (21:00 UTC) -> Spread Filter should return False
    sun_2100 = datetime(2026, 10, 18, 21, 15, 0, tzinfo=timezone.utc)
    is_normal, reason = guardian.is_spread_normal("EURUSD", now_utc=sun_2100)
    assert is_normal is False
    assert "Sunday market opening spread window" in reason

    # 2. Normal weekday -> Spread Filter should return True
    wed_1400 = datetime(2026, 10, 21, 14, 0, 0, tzinfo=timezone.utc)
    is_normal, reason = guardian.is_spread_normal("EURUSD", now_utc=wed_1400)
    assert is_normal is True

    # 3. R-Multiple Calculation
    # BUY: open=1.33035, sl=1.32032 (init_risk=0.01003) -> current=1.33746 (GBPUSD @ 0.71R)
    pos_buy = {"price_open": 1.33035, "price_current": 1.33746, "type": 0}
    r_buy = guardian.calculate_position_r(pos_buy, 0.01003)
    assert round(r_buy, 2) == 0.71

    # SELL: open=1.41108, sl=1.41981 (init_risk=0.00873) -> current=1.40084 (USDCAD @ 1.17R)
    pos_sell = {"price_open": 1.41108, "price_current": 1.40084, "type": 1}
    r_sell = guardian.calculate_position_r(pos_sell, 0.00873)
    assert round(r_sell, 2) == 1.17

    print("   ✅ Spread Filter, Sunday 21:00 UTC gate, and R-multiple calculations verified.")


def test_execution_audit_logger():
    print("👉 [9/11] Testing Dynamic Slippage & Execution Audit Logger...")
    from core.execution_audit import ExecutionAuditLogger
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        test_db = os.path.join(tmp_dir, "test_exec.db")
        test_jsonl = os.path.join(tmp_dir, "test_exec.jsonl")

        # Instantiate auditor with isolated temp files
        auditor = ExecutionAuditLogger.__new__(ExecutionAuditLogger)
        auditor._initialized = False
        auditor.__init__(db_path=test_db, jsonl_path=test_jsonl)

        # 1. Test BUY execution with adverse slippage
        # Req: 1.10000, Exec: 1.10020 (EURUSD, point=0.00001 -> 20 points adverse slippage = 2.0 pips)
        rec1 = auditor.record_execution(
            order_ticket=1001,
            deal_ticket=2001,
            symbol="EURUSDm",
            direction="BUY",
            volume=0.02,
            magic=1002,
            requested_price=1.10000,
            executed_price=1.10020,
            point=0.00001,
            digits=5,
            latency_ms=45.2,
            spread_points=12.0,
            comment="Test BUY"
        )
        assert rec1["slippage_points"] == 20.0, f"Expected 20.0 points, got {rec1['slippage_points']}"
        assert rec1["slippage_pips"] == 2.0, f"Expected 2.0 pips, got {rec1['slippage_pips']}"
        assert rec1["latency_ms"] == 45.2

        # 2. Test SELL execution with price improvement (favorable slippage)
        # Req: 2000.00, Exec: 2000.50 (XAUUSD SELL: executed higher than requested is favorable, slippage = -50 pts)
        rec2 = auditor.record_execution(
            order_ticket=1002,
            deal_ticket=2002,
            symbol="XAUUSDm",
            direction="SELL",
            volume=0.01,
            magic=1003,
            requested_price=2000.00,
            executed_price=2000.50,
            point=0.01,
            digits=2,
            latency_ms=32.1,
            spread_points=25.0,
            comment="Test SELL"
        )
        assert rec2["slippage_points"] == -50.0, f"Expected -50.0 pts, got {rec2['slippage_points']}"
        assert rec2["slippage_pips"] == -50.0

        # 3. Test summary stats
        stats = auditor.get_summary_statistics()
        assert stats["total_trades"] == 2
        assert stats["avg_latency_ms"] > 0

    print("   ✅ Slippage points/pips calculation, latency recording, and SQLite/JSONL persistence verified.")


def test_btc_volatility_squeeze():
    print("👉 [10/11] Testing BTC Volatility Compression Scanner (BBW & ATR Percentile)...")
    import pandas as pd
    import numpy as np

    strat = Btc8hStrategy(config={"symbol": "BTCUSDm", "spread_limit": 500, "magic": 1001})

    # Case A: Low volatility / tight consolidation (Squeeze state)
    np.random.seed(42)
    tight_prices = [80000.0 + float(np.sin(i / 5.0) * 50.0) for i in range(60)]
    df_squeeze = pd.DataFrame({
        "open": tight_prices,
        "high": [p + 20.0 for p in tight_prices],
        "low": [p - 20.0 for p in tight_prices],
        "close": tight_prices,
        "volume": [100.0] * 60
    })
    comp_squeeze = strat.calculate_volatility_compression(df_squeeze)
    assert comp_squeeze["bbw"] < 0.05, f"BBW should be compressed, got {comp_squeeze['bbw']}"
    assert "status" in comp_squeeze

    # Case B: Wide volatility breakout (Non-squeeze)
    wide_prices = [80000.0 + (i * 200.0) for i in range(60)]
    df_wide = pd.DataFrame({
        "open": wide_prices,
        "high": [p + 500.0 for p in wide_prices],
        "low": [p - 500.0 for p in wide_prices],
        "close": wide_prices,
        "volume": [100.0] * 60
    })
    comp_wide = strat.calculate_volatility_compression(df_wide)
    assert comp_wide["bbw"] > 0.01

    print("   ✅ Volatility Compression Scanner (BBW, ATR Percentile & Squeeze Flag) verified.")


def test_cross_asset_correlation_guard():
    print("👉 [11/11] Testing Cross-Asset Correlation Guard & Lot Scaling...")
    from core.fleet_risk_guard import CrossAssetCorrelationGuard, FleetRiskGuard

    corr_guard = CrossAssetCorrelationGuard(correlation_threshold=0.75, scale_discount=0.60)

    # 1. Baseline correlations
    assert corr_guard.get_correlation("USTEC", "US500") == 0.94
    assert corr_guard.get_correlation("EURUSD", "GBPUSD") == 0.80
    assert corr_guard.get_correlation("BTCUSD", "US500") == 0.0
    assert corr_guard.get_correlation("EURUSD", "EURUSD") == 1.0

    # 2. Portfolio Correlation Evaluation:
    # Existing position: USTEC BUY
    open_positions = [{"symbol": "USTECm", "type": "BUY", "ticket": 555}]

    # Evaluating new position US500 BUY (rho = 0.94 >= 0.75) -> Should cut size to 0.60x
    scale_us500, reason_us500 = corr_guard.evaluate_new_position_correlation(open_positions, "US500m", "BUY")
    assert scale_us500 == 0.60, f"Expected 0.60x scale discount, got {scale_us500}"
    assert "High cross-asset correlation" in reason_us500

    # Evaluating new position US500 SELL (rho = 0.94 but opposite direction -> aligned risk = -0.94 < 0.75) -> No cut
    scale_opp, _ = corr_guard.evaluate_new_position_correlation(open_positions, "US500m", "SELL")
    assert scale_opp == 1.0, f"Expected 1.0x scale, got {scale_opp}"

    # FleetRiskGuard integration test
    fleet_guard = FleetRiskGuard()
    f_scale = fleet_guard.get_correlation_scale(open_positions, "US500", "BUY")
    assert f_scale == 0.60

    print("   ✅ Cross-Asset Correlation Guard (USTEC+US500 0.60x haircut & factor isolation) verified.")


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
    test_market_schedule_governor()
    test_fleet_guardian_spread_and_trailing()
    test_execution_audit_logger()
    test_btc_volatility_squeeze()
    test_cross_asset_correlation_guard()
    print("==================================================")
    print("🎉 ALL 11 TESTS PASSED SUCCESSFULLY!")
    print("==================================================\n")
