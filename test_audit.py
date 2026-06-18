"""
测试脚本：调度决策审计与异常回溯模块
包含以下测试场景：
1. 审计日志生成验证
2. 决策分支记录验证
3. 异常检测功能验证
4. 决策对比功能验证
5. API接口功能验证
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import config
from models import (
    MicrogridState, SourceReport, LoadReport,
    AuditLog, InputSnapshot, DecisionBranch, OutputSummary, AnomalyMarker
)
from dispatcher import DispatchEngine
from audit import AuditBuilder, AnomalyDetector, DecisionComparator


@pytest.fixture
def state():
    return MicrogridState()


@pytest.fixture
def engine(state):
    return DispatchEngine(state)


@pytest.fixture
def populated_state(state):
    now = datetime.now()
    state.report_source(SourceReport(
        source_id="pv1", source_type="pv", power_kw=80.0,
        available=True, timestamp=now
    ))
    state.report_source(SourceReport(
        source_id="pv2", source_type="pv", power_kw=70.0,
        available=True, timestamp=now
    ))
    state.report_source(SourceReport(
        source_id="wt1", source_type="wt", power_kw=30.0,
        available=True, timestamp=now
    ))
    state.report_source(SourceReport(
        source_id="ds1", source_type="diesel", power_kw=0.0,
        available=True, timestamp=now
    ))
    state.report_load(LoadReport(load_kw=200.0, timestamp=now))
    return state


class TestAuditLogModels:
    """测试审计日志数据模型"""

    def test_input_snapshot_creation(self):
        snapshot = InputSnapshot(
            pv_output={"pv1": 80.0, "pv2": 70.0},
            wt_output={"wt1": 30.0},
            diesel_available={"ds1": True},
            load_kw=200.0,
            bess_soc={"bes1": 0.5},
            grid_buy_price=0.8,
            feed_in_price=0.3,
            tariff_period="flat",
            hour=14,
            storage_strategy_active=True,
            storage_mode="normal",
            demand_response_active=False,
            active_backup_plans=[],
            source_health_status={"pv:pv1": "normal"}
        )
        assert snapshot.load_kw == 200.0
        assert snapshot.bess_soc["bes1"] == 0.5
        assert snapshot.tariff_period == "flat"

    def test_decision_branch_creation(self):
        branch = DecisionBranch(
            branch_name="电池放电决策",
            decision=True,
            reason="负荷缺口需要填补",
            details={"discharge_kw": 50.0, "remaining_load_before": 20.0}
        )
        assert branch.branch_name == "电池放电决策"
        assert branch.decision is True
        assert branch.details["discharge_kw"] == 50.0

    def test_output_summary_creation(self):
        summary = OutputSummary(
            load_served_kw=190.0,
            load_shed_kw=10.0,
            load_coverage_ratio=0.95,
            total_cost=50.5,
            pv_share_kw=150.0,
            wt_share_kw=30.0,
            diesel_share_kw=0.0,
            bess_discharge_kw=20.0,
            grid_import_kw=0.0,
            grid_export_kw=10.0
        )
        assert summary.load_coverage_ratio == 0.95
        assert summary.total_cost == 50.5

    def test_anomaly_marker_creation(self):
        anomaly = AnomalyMarker(
            anomaly_type="COST_VOLATILITY",
            severity="high",
            description="成本波动异常",
            details={"current_cost": 100.0, "previous_cost": 30.0, "ratio": 3.33}
        )
        assert anomaly.anomaly_type == "COST_VOLATILITY"
        assert anomaly.severity == "high"

    def test_audit_log_creation(self):
        snapshot = InputSnapshot(
            pv_output={"pv1": 80.0}, wt_output={"wt1": 30.0},
            diesel_available={"ds1": True}, load_kw=150.0,
            bess_soc={"bes1": 0.5}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        branch = DecisionBranch(
            branch_name="购电决策", decision=True,
            reason="电价低于柴油成本", details={}
        )
        summary = OutputSummary(
            load_served_kw=150.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=10.0,
            pv_share_kw=80.0, wt_share_kw=30.0, diesel_share_kw=0.0,
            bess_discharge_kw=20.0, grid_import_kw=20.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=datetime.now(),
            input_snapshot=snapshot,
            decision_branches=[branch],
            output_summary=summary
        )
        assert audit.audit_id == "AUDIT-00000001"
        assert audit.has_anomaly() is False
        assert len(audit.reasoning_chain) == 0


class TestAuditBuilder:
    """测试审计日志构建器"""

    def test_capture_input_snapshot(self, populated_state):
        now = datetime.now()
        builder = AuditBuilder(populated_state, "DISP-00000001", now)
        snapshot = builder.capture_input_snapshot()

        assert snapshot.pv_output["pv1"] == 80.0
        assert snapshot.pv_output["pv2"] == 70.0
        assert snapshot.wt_output["wt1"] == 30.0
        assert snapshot.diesel_available["ds1"] is True
        assert snapshot.load_kw == 200.0
        assert snapshot.tariff_period == config.get_tariff_period(now.hour)

    def test_add_branch(self, state):
        now = datetime.now()
        builder = AuditBuilder(state, "DISP-00000001", now)
        builder.add_branch("测试分支", True, "测试原因", {"key": "value"})

        assert len(builder.decision_branches) == 1
        assert builder.decision_branches[0].branch_name == "测试分支"
        assert builder.decision_branches[0].decision is True
        assert len(builder.reasoning_chain) == 1
        assert "测试分支" in builder.reasoning_chain[0]


class TestAnomalyDetector:
    """测试异常检测器"""

    def test_check_diesel_in_valley_hours(self, state):
        detector = AnomalyDetector(state)

        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=100.0, bess_soc={}, grid_buy_price=0.4,
            feed_in_price=0.3, tariff_period="valley", hour=2,
            storage_strategy_active=True, storage_mode="active_charge",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=100.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=50.0,
            pv_share_kw=0.0, wt_share_kw=0.0, diesel_share_kw=50.0,
            bess_discharge_kw=0.0, grid_import_kw=50.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=datetime.now(),
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        anomaly = detector._check_diesel_in_valley_hours(audit)
        assert anomaly is not None
        assert anomaly.anomaly_type == "DIESEL_IN_VALLEY_HOURS"
        assert anomaly.severity == "medium"

    def test_check_diesel_in_valley_hours_no_diesel(self, state):
        detector = AnomalyDetector(state)

        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=100.0, bess_soc={}, grid_buy_price=0.4,
            feed_in_price=0.3, tariff_period="valley", hour=2,
            storage_strategy_active=True, storage_mode="active_charge",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=100.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=20.0,
            pv_share_kw=0.0, wt_share_kw=0.0, diesel_share_kw=0.0,
            bess_discharge_kw=0.0, grid_import_kw=100.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=datetime.now(),
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        anomaly = detector._check_diesel_in_valley_hours(audit)
        assert anomaly is None

    def test_check_diesel_in_peak_hours(self, state):
        detector = AnomalyDetector(state)

        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=100.0, bess_soc={}, grid_buy_price=1.2,
            feed_in_price=0.3, tariff_period="peak", hour=12,
            storage_strategy_active=True, storage_mode="priority_discharge",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=100.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=100.0,
            pv_share_kw=0.0, wt_share_kw=0.0, diesel_share_kw=50.0,
            bess_discharge_kw=50.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=datetime.now(),
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        anomaly = detector._check_diesel_in_valley_hours(audit)
        assert anomaly is None

    def test_check_consecutive_load_shedding(self, state):
        now = datetime.now()

        for i in range(4, -1, -1):
            summary = OutputSummary(
                load_served_kw=250.0, load_shed_kw=50.0 if i < 4 else 0.0,
                load_coverage_ratio=0.833, total_cost=100.0,
                pv_share_kw=100.0, wt_share_kw=50.0, diesel_share_kw=50.0,
                bess_discharge_kw=50.0, grid_import_kw=0.0, grid_export_kw=0.0
            )
            snapshot = InputSnapshot(
                pv_output={}, wt_output={}, diesel_available={},
                load_kw=300.0, bess_soc={}, grid_buy_price=0.8,
                feed_in_price=0.3, tariff_period="flat", hour=14,
                storage_strategy_active=True, storage_mode="normal",
                demand_response_active=False, active_backup_plans=[],
                source_health_status={}
            )
            audit = AuditLog(
                audit_id=f"AUDIT-{1000+i:08d}",
                dispatch_id=f"DISP-{1000+i:08d}",
                timestamp=now - timedelta(minutes=i),
                input_snapshot=snapshot,
                decision_branches=[],
                output_summary=summary
            )
            state.audit_logs.append(audit)

        detector = AnomalyDetector(state)

        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=300.0, bess_soc={}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=250.0, load_shed_kw=50.0,
            load_coverage_ratio=0.833, total_cost=100.0,
            pv_share_kw=100.0, wt_share_kw=50.0, diesel_share_kw=50.0,
            bess_discharge_kw=50.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now,
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        anomaly = detector._check_consecutive_load_shedding(audit)
        assert anomaly is not None
        assert anomaly.anomaly_type == "CONSECUTIVE_LOAD_SHEDDING"
        assert anomaly.severity == "critical"
        assert anomaly.details["consecutive_count"] >= 3

    def test_check_consecutive_load_shedding_no_shed(self, state):
        now = datetime.now()

        for i in range(3):
            decision = MagicMock()
            decision.load_shed_kw = 0.0
            decision.timestamp = now - timedelta(minutes=i)
            state.dispatch_history.append(decision)

        detector = AnomalyDetector(state)

        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=100.0, bess_soc={}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=100.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=10.0,
            pv_share_kw=100.0, wt_share_kw=0.0, diesel_share_kw=0.0,
            bess_discharge_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now,
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        anomaly = detector._check_consecutive_load_shedding(audit)
        assert anomaly is None


class TestDecisionComparator:
    """测试决策对比功能"""

    def test_compare_inputs(self):
        snapshot1 = InputSnapshot(
            pv_output={"pv1": 80.0, "pv2": 70.0},
            wt_output={"wt1": 30.0},
            diesel_available={"ds1": True},
            load_kw=200.0,
            bess_soc={"bes1": 0.6},
            grid_buy_price=0.8,
            feed_in_price=0.3,
            tariff_period="flat",
            hour=14,
            storage_strategy_active=True,
            storage_mode="normal",
            demand_response_active=False,
            active_backup_plans=[],
            source_health_status={"pv:pv1": "normal"}
        )
        snapshot2 = InputSnapshot(
            pv_output={"pv1": 80.0, "pv2": 70.0},
            wt_output={"wt1": 30.0},
            diesel_available={"ds1": True},
            load_kw=350.0,
            bess_soc={"bes1": 0.6},
            grid_buy_price=0.8,
            feed_in_price=0.3,
            tariff_period="flat",
            hour=14,
            storage_strategy_active=True,
            storage_mode="normal",
            demand_response_active=False,
            active_backup_plans=[],
            source_health_status={"pv:pv1": "normal"}
        )

        diffs = DecisionComparator._compare_inputs(snapshot1, snapshot2)
        assert "load_kw" in diffs
        assert diffs["load_kw"]["value1"] == 200.0
        assert diffs["load_kw"]["value2"] == 350.0
        assert diffs["load_kw"]["change"]["absolute"] == 150.0

    def test_compare_audits(self):
        now = datetime.now()
        snapshot1 = InputSnapshot(
            pv_output={"pv1": 80.0, "pv2": 70.0},
            wt_output={"wt1": 30.0},
            diesel_available={"ds1": True},
            load_kw=200.0,
            bess_soc={"bes1": 0.6},
            grid_buy_price=0.8,
            feed_in_price=0.3,
            tariff_period="flat",
            hour=14,
            storage_strategy_active=True,
            storage_mode="normal",
            demand_response_active=False,
            active_backup_plans=[],
            source_health_status={}
        )
        summary1 = OutputSummary(
            load_served_kw=200.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=20.0,
            pv_share_kw=150.0, wt_share_kw=30.0, diesel_share_kw=0.0,
            bess_discharge_kw=20.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit1 = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now - timedelta(minutes=5),
            input_snapshot=snapshot1,
            decision_branches=[],
            output_summary=summary1
        )

        snapshot2 = InputSnapshot(
            pv_output={"pv1": 80.0, "pv2": 70.0},
            wt_output={"wt1": 30.0},
            diesel_available={"ds1": True},
            load_kw=350.0,
            bess_soc={"bes1": 0.6},
            grid_buy_price=0.8,
            feed_in_price=0.3,
            tariff_period="flat",
            hour=14,
            storage_strategy_active=True,
            storage_mode="normal",
            demand_response_active=False,
            active_backup_plans=[],
            source_health_status={}
        )
        summary2 = OutputSummary(
            load_served_kw=350.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=140.0,
            pv_share_kw=150.0, wt_share_kw=30.0, diesel_share_kw=0.0,
            bess_discharge_kw=70.0, grid_import_kw=100.0, grid_export_kw=0.0
        )
        audit2 = AuditLog(
            audit_id="AUDIT-00000002",
            dispatch_id="DISP-00000002",
            timestamp=now,
            input_snapshot=snapshot2,
            decision_branches=[],
            output_summary=summary2
        )

        comparison = DecisionComparator.compare_audits(audit1, audit2)
        assert comparison["audit1_id"] == "AUDIT-00000001"
        assert comparison["audit2_id"] == "AUDIT-00000002"
        assert "load_kw" in comparison["input_differences"]
        assert "grid_import_kw" in comparison["output_differences"]
        assert len(comparison["causal_analysis"]) >= 0


class TestMicrogridStateAuditMethods:
    """测试MicrogridState的审计相关方法"""

    def test_generate_ids(self, state):
        dispatch_id1 = state.generate_dispatch_id()
        dispatch_id2 = state.generate_dispatch_id()
        assert dispatch_id1 == "DISP-00000001"
        assert dispatch_id2 == "DISP-00000002"

        audit_id1 = state.generate_audit_id()
        audit_id2 = state.generate_audit_id()
        assert audit_id1 == "AUDIT-00000001"
        assert audit_id2 == "AUDIT-00000002"

    def test_add_and_get_audit_log(self, state):
        now = datetime.now()
        snapshot = InputSnapshot(
            pv_output={}, wt_output={}, diesel_available={},
            load_kw=100.0, bess_soc={}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=100.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=10.0,
            pv_share_kw=100.0, wt_share_kw=0.0, diesel_share_kw=0.0,
            bess_discharge_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now,
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        state.add_audit_log(audit)
        assert len(state.audit_logs) == 1

        retrieved = state.get_audit_log("AUDIT-00000001")
        assert retrieved is not None
        assert retrieved.audit_id == "AUDIT-00000001"

        retrieved_by_dispatch = state.get_audit_log_by_dispatch_id("DISP-00000001")
        assert retrieved_by_dispatch is not None

        not_found = state.get_audit_log("AUDIT-99999999")
        assert not_found is None

    def test_query_audit_logs(self, state):
        now = datetime.now()

        for i in range(5):
            snapshot = InputSnapshot(
                pv_output={}, wt_output={}, diesel_available={},
                load_kw=100.0 + i * 20,
                bess_soc={}, grid_buy_price=0.8,
                feed_in_price=0.3, tariff_period="flat", hour=14,
                storage_strategy_active=True, storage_mode="normal",
                demand_response_active=False, active_backup_plans=[],
                source_health_status={}
            )
            summary = OutputSummary(
                load_served_kw=100.0 + i * 20 if i < 4 else 150.0,
                load_shed_kw=0.0 if i < 4 else 30.0,
                load_coverage_ratio=1.0 if i < 4 else 0.833,
                total_cost=10.0 * (i + 1),
                pv_share_kw=100.0, wt_share_kw=0.0, diesel_share_kw=0.0 if i < 2 else 20.0,
                bess_discharge_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0
            )
            audit = AuditLog(
                audit_id=f"AUDIT-{i+1:08d}",
                dispatch_id=f"DISP-{i+1:08d}",
                timestamp=now - timedelta(minutes=i * 5),
                input_snapshot=snapshot,
                decision_branches=[],
                output_summary=summary
            )
            state.add_audit_log(audit)

        all_logs = state.query_audit_logs(limit=10)
        assert len(all_logs) == 5

        high_cost_logs = state.query_audit_logs(min_cost=30.0)
        assert len(high_cost_logs) == 3

        shed_logs = state.query_audit_logs(has_load_shed=True)
        assert len(shed_logs) == 1

        diesel_logs = state.query_audit_logs(has_diesel_start=True)
        assert len(diesel_logs) == 3

    def test_get_anomaly_audit_logs(self, state):
        now = datetime.now()

        for i in range(5):
            snapshot = InputSnapshot(
                pv_output={}, wt_output={}, diesel_available={},
                load_kw=100.0, bess_soc={}, grid_buy_price=0.8,
                feed_in_price=0.3, tariff_period="flat", hour=14,
                storage_strategy_active=True, storage_mode="normal",
                demand_response_active=False, active_backup_plans=[],
                source_health_status={}
            )
            summary = OutputSummary(
                load_served_kw=100.0, load_shed_kw=0.0,
                load_coverage_ratio=1.0, total_cost=10.0,
                pv_share_kw=100.0, wt_share_kw=0.0, diesel_share_kw=0.0,
                bess_discharge_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0
            )
            anomalies = []
            if i % 2 == 0:
                anomalies.append(AnomalyMarker(
                    anomaly_type="TEST_ANOMALY",
                    severity="high",
                    description="测试异常",
                    details={}
                ))
            audit = AuditLog(
                audit_id=f"AUDIT-{i+1:08d}",
                dispatch_id=f"DISP-{i+1:08d}",
                timestamp=now - timedelta(minutes=i * 5),
                input_snapshot=snapshot,
                decision_branches=[],
                output_summary=summary,
                anomalies=anomalies
            )
            state.add_audit_log(audit)

        anomalies = state.get_anomaly_audit_logs()
        assert len(anomalies) == 3
        for a in anomalies:
            assert a.has_anomaly() is True


class TestDispatcherIntegration:
    """测试调度器与审计模块的集成"""

    def test_dispatch_generates_audit_log(self, populated_state, engine):
        now = datetime.now()
        decision = engine.execute(now)

        assert len(populated_state.audit_logs) == 1
        audit = populated_state.audit_logs[0]

        assert audit.dispatch_id.startswith("DISP-")
        assert audit.audit_id.startswith("AUDIT-")
        assert audit.timestamp == now

        assert audit.input_snapshot.load_kw == 200.0
        assert audit.input_snapshot.pv_output["pv1"] == 80.0

        assert len(audit.decision_branches) > 0

        branch_names = [b.branch_name for b in audit.decision_branches]
        assert "储能计划生成" in branch_names
        assert "电池SOC异常处理" in branch_names
        assert "电价策略判定" in branch_names
        assert "储能策略模式" in branch_names
        assert "故障预案触发" in branch_names

        assert audit.output_summary.total_cost >= 0
        assert audit.output_summary.load_coverage_ratio <= 1.0
        assert audit.output_summary.load_coverage_ratio >= 0

        assert len(audit.reasoning_chain) > 0


class TestAPIFunctions:
    """测试API序列化函数"""

    def test_audit_to_dict_brief(self):
        from app import _audit_to_dict_brief

        now = datetime.now()
        snapshot = InputSnapshot(
            pv_output={"pv1": 80.0}, wt_output={"wt1": 30.0},
            diesel_available={"ds1": True}, load_kw=200.0,
            bess_soc={"bes1": 0.5}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={}
        )
        summary = OutputSummary(
            load_served_kw=200.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=20.5,
            pv_share_kw=110.0, wt_share_kw=30.0, diesel_share_kw=0.0,
            bess_discharge_kw=60.0, grid_import_kw=0.0, grid_export_kw=0.0
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now,
            input_snapshot=snapshot,
            decision_branches=[],
            output_summary=summary
        )

        result = _audit_to_dict_brief(audit)
        assert result["audit_id"] == "AUDIT-00000001"
        assert result["total_cost"] == 20.5
        assert result["has_anomaly"] is False
        assert result["has_load_shed"] is False
        assert result["tariff_period"] == "flat"

    def test_audit_to_dict_detail(self):
        from app import _audit_to_dict_detail

        now = datetime.now()
        snapshot = InputSnapshot(
            pv_output={"pv1": 80.0}, wt_output={"wt1": 30.0},
            diesel_available={"ds1": True}, load_kw=200.0,
            bess_soc={"bes1": 0.5}, grid_buy_price=0.8,
            feed_in_price=0.3, tariff_period="flat", hour=14,
            storage_strategy_active=True, storage_mode="normal",
            demand_response_active=False, active_backup_plans=[],
            source_health_status={"pv:pv1": "normal"}
        )
        branch = DecisionBranch(
            branch_name="购电决策", decision=True,
            reason="电价低于柴油成本", details={"grid_import_kw": 50.0}
        )
        summary = OutputSummary(
            load_served_kw=200.0, load_shed_kw=0.0,
            load_coverage_ratio=1.0, total_cost=20.5,
            pv_share_kw=110.0, wt_share_kw=30.0, diesel_share_kw=0.0,
            bess_discharge_kw=60.0, grid_import_kw=0.0, grid_export_kw=0.0,
            cost_breakdown={"grid_import_cost": 10.0, "diesel_generation_cost": 0.0}
        )
        audit = AuditLog(
            audit_id="AUDIT-00000001",
            dispatch_id="DISP-00000001",
            timestamp=now,
            input_snapshot=snapshot,
            decision_branches=[branch],
            output_summary=summary,
            reasoning_chain=["[购电决策] 是 - 电价低于柴油成本"]
        )

        result = _audit_to_dict_detail(audit)
        assert result["audit_id"] == "AUDIT-00000001"
        assert "input_snapshot" in result
        assert "decision_branches" in result
        assert "output_summary" in result
        assert "reasoning_chain" in result
        assert len(result["decision_branches"]) == 1
        assert result["decision_branches"][0]["branch_name"] == "购电决策"
        assert result["decision_branches"][0]["decision_text"] == "是"
        assert "cost_breakdown" in result["output_summary"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
