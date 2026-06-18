import pytest
from datetime import datetime, timedelta

import config
from models import MicrogridState, SourceReport, LoadReport
from price_forecast import PriceForecastManager
from dispatcher import DispatchEngine


@pytest.fixture
def state():
    s = MicrogridState()
    s.report_source(SourceReport(
        source_id="pv1", source_type="pv",
        power_kw=50.0, available=True, timestamp=datetime.now()
    ))
    s.report_source(SourceReport(
        source_id="pv2", source_type="pv",
        power_kw=50.0, available=True, timestamp=datetime.now()
    ))
    s.report_source(SourceReport(
        source_id="wt1", source_type="wt",
        power_kw=20.0, available=True, timestamp=datetime.now()
    ))
    s.report_load(LoadReport(load_kw=200.0, timestamp=datetime.now()))
    return s


@pytest.fixture
def pf_manager(state):
    return PriceForecastManager(state)


@pytest.fixture
def engine(state, pf_manager):
    return DispatchEngine(state, price_forecast_manager=pf_manager)


class TestPriceForecastManager:
    def test_submit_forecast_success(self, pf_manager):
        prices = [0.35] * 24
        result = pf_manager.submit_forecast(prices)

        assert result["forecast"] is not None
        assert result["comparison"] is not None
        assert result["strategy"] is not None
        assert result["forecast"].status == "pending"
        assert len(result["forecast"].prices) == 24
        assert result["comparison"].forecast_id == result["forecast"].forecast_id
        assert result["strategy"].forecast_id == result["forecast"].forecast_id

    def test_submit_forecast_invalid_length(self, pf_manager):
        with pytest.raises(ValueError, match="必须提供24个时段的电价数据"):
            pf_manager.submit_forecast([0.3] * 23)

    def test_submit_forecast_negative_price(self, pf_manager):
        prices = [0.3] * 24
        prices[12] = -0.1
        with pytest.raises(ValueError, match="不能为负数"):
            pf_manager.submit_forecast(prices)

    def test_submit_forecast_override_same_date(self, pf_manager):
        prices1 = [0.35] * 24
        result1 = pf_manager.submit_forecast(prices1, "2024-01-01")

        prices2 = [0.4] * 24
        result2 = pf_manager.submit_forecast(prices2, "2024-01-01")

        forecast1 = pf_manager.get_forecast(result1["forecast"].forecast_id)
        assert forecast1.status == "expired"

        forecast2 = pf_manager.get_forecast(result2["forecast"].forecast_id)
        assert forecast2.status == "pending"

    def test_generate_comparison_valley_opportunity(self, pf_manager):
        valley_price = config.get_valley_price()
        prices = [valley_price - 0.1] * 24
        result = pf_manager.submit_forecast(prices)

        comparison = result["comparison"]
        assert len(comparison.valley_opportunity_hours) == 24
        assert comparison.total_valley_savings_potential > 0

    def test_generate_comparison_peak_risk(self, pf_manager):
        peak_price = config.get_peak_price()
        prices = [peak_price * 2.0] * 24
        result = pf_manager.submit_forecast(prices)

        comparison = result["comparison"]
        assert len(comparison.peak_risk_hours) == 24
        assert comparison.total_peak_risk_cost > 0

    def test_generate_strategy_active_charge(self, pf_manager):
        valley_price = config.get_valley_price()
        prices = [valley_price - 0.1] * 24
        result = pf_manager.submit_forecast(prices)

        strategy = result["strategy"]
        for hour in strategy.hours:
            assert hour.suggested_action == "active_charge"
        assert strategy.summary["active_charge_hours"] == 24

    def test_generate_strategy_force_discharge(self, pf_manager):
        peak_price = config.get_peak_price()
        force_threshold = peak_price * 1.5
        prices = [force_threshold + 0.5] * 24
        result = pf_manager.submit_forecast(prices)

        strategy = result["strategy"]
        for hour in strategy.hours:
            assert hour.suggested_action == "force_discharge_no_grid"
        assert strategy.summary["force_discharge_hours"] == 24

    def test_generate_strategy_normal(self, pf_manager):
        flat_price = config.get_flat_price()
        prices = [flat_price] * 24
        result = pf_manager.submit_forecast(prices)

        strategy = result["strategy"]
        assert strategy.summary["normal_hours"] > 0

    def test_activate_strategy(self, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices)
        forecast_id = result["forecast"].forecast_id

        success = pf_manager.activate_strategy(forecast_id)
        assert success is True

        active_strategy = pf_manager.get_active_strategy()
        assert active_strategy is not None
        assert active_strategy.status == "active"

        active_forecast = pf_manager.get_active_forecast()
        assert active_forecast is not None
        assert active_forecast.status == "active"
        assert active_forecast.forecast_id == forecast_id

    def test_activate_already_active(self, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices)
        forecast_id = result["forecast"].forecast_id

        pf_manager.activate_strategy(forecast_id)
        success = pf_manager.activate_strategy(forecast_id)
        assert success is False

    def test_deactivate_strategy(self, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices)
        forecast_id = result["forecast"].forecast_id

        pf_manager.activate_strategy(forecast_id)
        success = pf_manager.deactivate_strategy(forecast_id)
        assert success is True

        active_strategy = pf_manager.get_active_strategy()
        assert active_strategy is None

        forecast = pf_manager.get_forecast(forecast_id)
        assert forecast.status == "deactivated"

    def test_is_force_discharge_hour(self, pf_manager):
        peak_price = config.get_peak_price()
        force_threshold = peak_price * 1.5
        prices = [0.5] * 24
        prices[12] = force_threshold + 0.5
        prices[13] = force_threshold + 0.5

        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        assert pf_manager.is_force_discharge_hour(12) is True
        assert pf_manager.is_force_discharge_hour(13) is True
        assert pf_manager.is_force_discharge_hour(8) is False

    def test_get_effective_buy_price(self, pf_manager):
        prices = [0.3 + i * 0.05 for i in range(24)]
        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        assert abs(pf_manager.get_effective_buy_price(0) - 0.3) < 0.001
        assert abs(pf_manager.get_effective_buy_price(5) - 0.55) < 0.001

    def test_get_forecast_by_date(self, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices, "2024-06-18")

        forecast = pf_manager.get_forecast_by_date("2024-06-18")
        assert forecast is not None
        assert forecast.forecast_date == "2024-06-18"

        forecast_none = pf_manager.get_forecast_by_date("2024-01-01")
        assert forecast_none is None

    def test_check_and_expire_strategy(self, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices, "2020-01-01")
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        expired = pf_manager.check_and_expire_strategy(datetime(2024, 1, 2))
        assert expired is True

        active_strategy = pf_manager.get_active_strategy()
        assert active_strategy is None

    def test_execution_stats(self, pf_manager):
        pf_manager.record_execution_stats(
            "2024-01-01", True, "STRAT-001",
            1000.0, 500.0, 800.0, 0.0
        )
        pf_manager.record_execution_stats(
            "2024-01-02", False, None,
                1000.0, 800.0, 800.0, 0.0
        )

        stats = pf_manager.get_execution_stats()
        assert stats.strategy_days == 1
        assert stats.no_strategy_days == 1
        assert stats.avg_cost_with_strategy > 0
        assert stats.avg_cost_without_strategy > 0


class TestDispatchWithPriceForecast:
    def test_dispatch_with_active_forecast_price_used(self, state, engine, pf_manager):
        valley_price = config.get_valley_price()
        forecast_price = valley_price - 0.1
        prices = [forecast_price] * 24

        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        now = datetime(2024, 6, 18, 2, 0, 0)
        decision = engine.execute(now)

        assert decision.grid_buy_price == forecast_price
        assert any("动态电价生效" in note for note in decision.notes)

    def test_dispatch_force_discharge_no_grid(self, state, engine, pf_manager):
        peak_price = config.get_peak_price()
        force_price = peak_price * 2.0
        prices = [0.5] * 24
        prices[12] = force_price

        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        state.bess_state["bes1"].soc = 0.8
        state.report_load(LoadReport(load_kw=500.0, timestamp=datetime.now()))

        now = datetime(2024, 6, 18, 12, 0, 0)
        decision = engine.execute(now)

        assert any("强制放电" in note or "高价风险时段" in note for note in decision.notes)
        assert decision.bess_action["bes1"]["discharge_kw"] > 0

    def test_dispatch_active_charge_with_dynamic_price(self, state, engine, pf_manager):
        valley_price = config.get_valley_price()
        forecast_price = valley_price - 0.1
        prices = [forecast_price] * 24

        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        state.bess_state["bes1"].soc = 0.3

        now = datetime(2024, 6, 18, 10, 0, 0)
        decision = engine.execute(now)

        assert decision.bess_action["bes1"]["charge_kw"] > 0
        assert any("主动充电" in note for note in decision.notes)

    def test_dispatch_no_forecast_uses_fixed_price(self, state, engine, pf_manager):
        now = datetime(2024, 6, 18, 12, 0, 0)
        decision = engine.execute(now)

        period = config.get_tariff_period(now.hour)
        fixed_price = config.GRID_TARIFF[period]["price"]
        assert decision.grid_buy_price == fixed_price

    def test_strategy_activation_updates_storage_plan(self, state, pf_manager):
        valley_price = config.get_valley_price()
        prices = [valley_price - 0.1] * 24

        result = pf_manager.submit_forecast(prices)

        assert state.current_storage_plan is None

        pf_manager.activate_strategy(result["forecast"].forecast_id)

        assert state.current_storage_plan is not None
        assert state.current_storage_plan.plan_date == result["forecast"].forecast_date
        assert len(state.current_storage_plan.hours) == 24

        for hour in range(24):
            plan_hour = state.current_storage_plan.hours.get(hour)
            assert plan_hour is not None
            assert plan_hour.mode == "active_charge"

    def test_strategy_deactivation_restores_plan(self, state, pf_manager):
        prices = [0.5] * 24
        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        plan_after_activation = state.current_storage_plan
        assert plan_after_activation is not None

        pf_manager.deactivate_strategy(result["forecast"].forecast_id)

        assert state.current_storage_plan is not None
        assert state.current_storage_plan != plan_after_activation


class TestEdgeCases:
    def test_multiple_forecasts_activation(self, pf_manager):
        prices1 = [0.4] * 24
        result1 = pf_manager.submit_forecast(prices1, "2024-01-01")

        prices2 = [0.5] * 24
        result2 = pf_manager.submit_forecast(prices2, "2024-01-02")

        pf_manager.activate_strategy(result1["forecast"].forecast_id)
        assert pf_manager.get_active_forecast().forecast_id == result1["forecast"].forecast_id

        pf_manager.activate_strategy(result2["forecast"].forecast_id)
        assert pf_manager.get_active_forecast().forecast_id == result2["forecast"].forecast_id

        forecast1 = pf_manager.get_forecast(result1["forecast"].forecast_id)
        assert forecast1.status == "deactivated"

    def test_get_hour_strategy(self, pf_manager):
        prices = [0.3 + i * 0.05 for i in range(24)]
        result = pf_manager.submit_forecast(prices)
        pf_manager.activate_strategy(result["forecast"].forecast_id)

        hour_strategy = pf_manager.get_hour_strategy(0)
        assert hour_strategy is not None
        assert hour_strategy.hour == 0
        assert hour_strategy.forecast_price == 0.3

        no_strategy = pf_manager.get_hour_strategy(25)
        assert no_strategy is None

    def test_execution_stats_date_filter(self, pf_manager):
        for i in range(10):
            date_str = f"2024-01-{i+1:02d}"
            pf_manager.record_execution_stats(
                date_str, i % 2 == 0, f"STRAT-{i:03d}",
                1000.0, 500.0 + i * 10, 900.0, 0.0
            )

        stats = pf_manager.get_execution_stats("2024-01-03", "2024-01-07")
        assert len(stats.details) == 5

        stats_all = pf_manager.get_execution_stats()
        assert len(stats_all.details) == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
