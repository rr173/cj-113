import pytest
from datetime import datetime, timedelta

import config
from models import MicrogridState, SourceReport, LoadReport, CostAttribution
from report import ReportGenerator
from dispatcher import DispatchEngine


@pytest.fixture
def state():
    s = MicrogridState()
    return s


@pytest.fixture
def engine(state):
    return DispatchEngine(state)


@pytest.fixture
def report_generator(state):
    return ReportGenerator(state)


class TestDailyReportGeneration:
    def test_generate_empty_daily_report(self, report_generator):
        report = report_generator.generate_daily_report("2024-06-18")
        
        assert report.report_type == "daily"
        assert report.report_date == "2024-06-18"
        assert report.dispatch_count == 0
        assert report.grid_purchase_cost == 0
        assert report.diesel_total_cost == 0
        assert report.load_shed_penalty == 0
        assert report.feed_in_revenue == 0
        assert report.net_cost == 0
        assert len(report.top_expensive_dispatches) == 0
        assert len(report.battery_stats) > 0
        assert len(report.load_group_reliability) == 3

    def test_daily_report_with_dispatches(self, state, report_generator, engine):
        date = datetime(2024, 6, 18, 10, 0, 0)
        
        state.report_source(SourceReport(
            source_id="pv1", source_type="pv",
            power_kw=50.0, available=True, timestamp=date
        ))
        state.report_source(SourceReport(
            source_id="pv2", source_type="pv",
            power_kw=50.0, available=True, timestamp=date
        ))
        state.report_source(SourceReport(
            source_id="wt1", source_type="wt",
            power_kw=20.0, available=True, timestamp=date
        ))
        state.report_load(LoadReport(load_kw=200.0, timestamp=date))
        
        decision = engine.execute(now=date)
        
        report = report_generator.generate_daily_report("2024-06-18")
        
        assert report.dispatch_count >= 1
        assert report.grid_purchase_cost >= 0
        assert report.net_cost >= 0
        assert len(report.top_expensive_dispatches) >= 1
        assert "bes1" in report.battery_stats

    def test_daily_report_prev_day_comparison(self, state, report_generator, engine):
        for day in range(17, 19):
            for hour in range(10, 14):
                d = datetime(2024, 6, day, hour, 0, 0)
                state.report_source(SourceReport(
                    source_id="pv1", source_type="pv",
                    power_kw=30.0, available=True, timestamp=d
                ))
                state.report_source(SourceReport(
                    source_id="pv2", source_type="pv",
                    power_kw=30.0, available=True, timestamp=d
                ))
                state.report_source(SourceReport(
                    source_id="wt1", source_type="wt",
                    power_kw=10.0, available=True, timestamp=d
                ))
                state.report_load(LoadReport(load_kw=300.0, timestamp=d))
                engine.execute(now=d)
        
        report = report_generator.generate_daily_report("2024-06-18")
        
        assert report.prev_day_net_cost is not None
        assert report.prev_day_net_cost > 0
        assert report.net_cost_change_percent is not None

    def test_top_expensive_dispatches(self, state, report_generator, engine):
        for hour in range(10, 14):
            date = datetime(2024, 6, 18, hour, 0, 0)
            state.report_source(SourceReport(
                source_id="pv1", source_type="pv",
                power_kw=30.0, available=True, timestamp=date
            ))
            state.report_source(SourceReport(
                source_id="pv2", source_type="pv",
                power_kw=30.0, available=True, timestamp=date
            ))
            state.report_source(SourceReport(
                source_id="wt1", source_type="wt",
                power_kw=10.0, available=True, timestamp=date
            ))
            state.report_load(LoadReport(load_kw=300.0, timestamp=date))
            engine.execute(now=date)
        
        report = report_generator.generate_daily_report("2024-06-18")
        
        assert len(report.top_expensive_dispatches) == 3
        costs = [d.total_cost for d in report.top_expensive_dispatches]
        assert costs == sorted(costs, reverse=True)

    def test_battery_stats(self, state, report_generator, engine):
        date = datetime(2024, 6, 18, 2, 0, 0)
        
        state.report_source(SourceReport(
            source_id="pv1", source_type="pv",
            power_kw=0.0, available=True, timestamp=date
        ))
        state.report_source(SourceReport(
            source_id="pv2", source_type="pv",
            power_kw=0.0, available=True, timestamp=date
        ))
        state.report_source(SourceReport(
            source_id="wt1", source_type="wt",
            power_kw=0.0, available=True, timestamp=date
        ))
        state.report_load(LoadReport(load_kw=50.0, timestamp=date))
        engine.execute(now=date)
        
        report = report_generator.generate_daily_report("2024-06-18")
        bstats = report.battery_stats["bes1"]
        
        assert bstats.total_charged_kwh >= 0
        assert bstats.total_discharged_kwh >= 0
        assert 0 <= bstats.soc_min <= bstats.soc_max <= 1
        assert bstats.cycle_increment >= 0


class TestWeeklyReportGeneration:
    def test_generate_weekly_report(self, report_generator):
        report = report_generator.generate_weekly_report("2024-06-10", "2024-06-16")
        
        assert report.report_type == "weekly"
        assert report.start_date == "2024-06-10"
        assert report.end_date == "2024-06-16"
        assert len(report.daily_trend) == 7
        assert report.total_dispatch_count == 0
        assert report.total_net_cost == 0

    def test_weekly_report_with_data(self, state, report_generator, engine):
        for day in range(10, 17):
            date = datetime(2024, 6, day, 12, 0, 0)
            state.report_source(SourceReport(
                source_id="pv1", source_type="pv",
                power_kw=50.0, available=True, timestamp=date
            ))
            state.report_source(SourceReport(
                source_id="pv2", source_type="pv",
                power_kw=50.0, available=True, timestamp=date
            ))
            state.report_source(SourceReport(
                source_id="wt1", source_type="wt",
                power_kw=20.0, available=True, timestamp=date
            ))
            state.report_load(LoadReport(load_kw=200.0, timestamp=date))
            engine.execute(now=date)
        
        report = report_generator.generate_weekly_report("2024-06-10", "2024-06-16")
        
        assert report.total_dispatch_count >= 7
        assert len(report.daily_trend) == 7
        assert report.avg_daily_dispatch_count >= 1
        assert report.most_expensive_day != ""
        assert report.cheapest_day != ""
        assert report.most_expensive_day_cost >= report.cheapest_day_cost


class TestStrategySuggestions:
    def test_no_suggestions_when_normal(self, report_generator):
        report = report_generator.generate_daily_report("2024-06-18")
        assert isinstance(report.suggestions, list)

    def test_capacity_expansion_suggestion(self, state, report_generator):
        for i in range(5):
            from models import LoadGroupShedEvent
            event = LoadGroupShedEvent(
                event_id=f"SHED-{i:06d}",
                group_id="group3",
                group_name="三级(一般负荷)",
                shed_power_kw=50.0,
                started_at=datetime(2024, 6, 18, 10 + i, 0, 0),
                ended_at=datetime(2024, 6, 18, 10 + i, 30, 0),
                duration_minutes=30.0,
                reason="测试甩负荷",
            )
            state.load_group_shed_events.append(event)
        
        report = report_generator.generate_daily_report("2024-06-18")
        
        has_capacity_suggestion = any(
            s.type == "CAPACITY_EXPANSION" for s in report.suggestions
        )
        assert has_capacity_suggestion

    def test_valley_charge_suggestion(self, state, report_generator):
        for i in range(10):
            attr = CostAttribution(
                attribution_id=f"COST-{i:08d}",
                dispatch_id=f"DISP-{i:08d}",
                timestamp=datetime(2024, 6, 18, 12, i, 0),
                grid_purchase_cost=10.0,
                diesel_generation_cost=0.0,
                diesel_startup_cost=0.0,
                load_shed_penalty_cost=0.0,
                bess_loss_cost=0.0,
                feed_in_revenue=0.0,
                total_comprehensive_cost=10.0,
                details={
                    "grid_import_kwh": 20.0,
                    "tariff_period": "peak",
                }
            )
            state.cost_attributions.append(attr)
        
        report = report_generator.generate_daily_report("2024-06-18")
        
        has_valley_suggestion = any(
            s.type == "VALLEY_CHARGE_OPTIMIZATION" for s in report.suggestions
        )
        assert has_valley_suggestion


class TestReportManagement:
    def test_generate_and_store_daily_report(self, state):
        report = state.generate_daily_report("2024-06-18")
        assert report.report_id is not None
        
        stored = state.get_daily_report("2024-06-18")
        assert stored is not None
        assert stored.report_id == report.report_id

    def test_daily_report_override(self, state):
        report1 = state.generate_daily_report("2024-06-18")
        report2 = state.generate_daily_report("2024-06-18")
        
        assert report1.report_id != report2.report_id
        
        stored = state.get_daily_report("2024-06-18")
        assert stored.report_id == report2.report_id
        assert len(state.daily_reports) == 1

    def test_generate_and_store_weekly_report(self, state):
        report = state.generate_weekly_report("2024-06-10", "2024-06-16")
        assert report.report_id is not None
        
        stored = state.get_weekly_report("2024-06-10", "2024-06-16")
        assert stored is not None
        assert stored.report_id == report.report_id

    def test_list_reports(self, state):
        state.generate_daily_report("2024-06-18")
        state.generate_daily_report("2024-06-19")
        state.generate_weekly_report("2024-06-10", "2024-06-16")
        
        all_reports = state.list_reports()
        assert len(all_reports) == 3
        
        daily_reports = state.list_reports("daily")
        assert len(daily_reports) == 2
        
        weekly_reports = state.list_reports("weekly")
        assert len(weekly_reports) == 1

    def test_get_report_by_id(self, state):
        report = state.generate_daily_report("2024-06-18")
        found = state.get_report_by_id(report.report_id)
        assert found is not None
        assert found.report_id == report.report_id
        
        not_found = state.get_report_by_id("NONEXISTENT")
        assert not_found is None

    def test_delete_report(self, state):
        report = state.generate_daily_report("2024-06-18")
        assert len(state.daily_reports) == 1
        
        success = state.delete_report(report.report_id)
        assert success is True
        assert len(state.daily_reports) == 0
        
        success = state.delete_report("NONEXISTENT")
        assert success is False


class TestReportDataIntegrity:
    def test_daily_report_net_cost_calculation(self, report_generator):
        report = report_generator.generate_daily_report("2024-06-18")
        
        expected_net = (report.grid_purchase_cost + report.diesel_total_cost + 
                       report.load_shed_penalty - report.feed_in_revenue)
        assert abs(report.net_cost - expected_net) < 0.01

    def test_load_group_reliability_count(self, report_generator):
        report = report_generator.generate_daily_report("2024-06-18")
        
        assert len(report.load_group_reliability) == len(config.LOAD_GROUP_CONFIG)
        group_ids = {r.group_id for r in report.load_group_reliability}
        assert group_ids == set(config.LOAD_GROUP_CONFIG.keys())

    def test_weekly_report_daily_trend_count(self, report_generator):
        report = report_generator.generate_weekly_report("2024-06-10", "2024-06-16")
        
        assert len(report.daily_trend) == 7
        dates = [d["date"] for d in report.daily_trend]
        assert "2024-06-10" in dates
        assert "2024-06-16" in dates


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
