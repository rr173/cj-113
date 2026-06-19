"""
单元测试：负荷分群管理与优先级供电模块
测试：
  1. 负荷群组配置初始化
  2. 分群上报与总负荷汇总
  3. 优先级甩负荷（三级→二级，一级不切）
  4. 恢复供电（一级→二级→三级）
  5. 群组状态查询
  6. 群组配置修改
  7. 切除事件历史
  8. 可靠性统计
  9. 完整调度流程集成测试
"""
import pytest
from datetime import datetime, timedelta

import config
from models import (
    MicrogridState,
    SourceReport,
    LoadReport,
    LoadGroupReport,
    DispatchDecision,
)
from dispatcher import DispatchEngine


@pytest.fixture
def state():
    s = MicrogridState()
    return s


@pytest.fixture
def engine(state):
    return DispatchEngine(state)


def _report_all_sources(state, pv1=0, pv2=0, wt1=0, diesel_avail=True, now=None):
    if now is None:
        now = datetime.now()
    state.report_source(SourceReport("pv1", "pv", pv1, True, now))
    state.report_source(SourceReport("pv2", "pv", pv2, True, now))
    state.report_source(SourceReport("wt1", "wt", wt1, True, now))
    state.report_source(SourceReport("ds1", "diesel", 0, diesel_avail, now))


def _report_load_groups(state, g1=50, g2=120, g3=180, now=None):
    if now is None:
        now = datetime.now()
    gr1 = LoadGroupReport(group_id="group1", actual_power_kw=g1, timestamp=now)
    gr2 = LoadGroupReport(group_id="group2", actual_power_kw=g2, timestamp=now)
    gr3 = LoadGroupReport(group_id="group3", actual_power_kw=g3, timestamp=now)
    total = g1 + g2 + g3
    report = LoadReport(
        load_kw=total,
        timestamp=now,
        group_reports={"group1": gr1, "group2": gr2, "group3": gr3},
    )
    state.report_load(report)


class Test01_GroupConfigInit:
    """测试1：负荷群组配置初始化"""

    def test_default_groups_exist(self, state):
        """三个预置群组均存在"""
        assert "group1" in state.load_group_state
        assert "group2" in state.load_group_state
        assert "group3" in state.load_group_state

    def test_group1_critical_config(self, state):
        """一级：关键负荷，50kW，不允许切除"""
        gs = state.load_group_state["group1"]
        assert gs["name"] == "一级(关键负荷)"
        assert gs["rated_power_kw"] == 50.0
        assert gs["max_shed_ratio"] == 0.0
        assert gs["shed_priority"] == 1
        assert gs["restore_priority"] == 1

    def test_group2_important_config(self, state):
        """二级：重要负荷，120kW，最多切60%"""
        gs = state.load_group_state["group2"]
        assert gs["name"] == "二级(重要负荷)"
        assert gs["rated_power_kw"] == 120.0
        assert gs["max_shed_ratio"] == 0.6
        assert gs["shed_priority"] == 2
        assert gs["restore_priority"] == 2

    def test_group3_general_config(self, state):
        """三级：一般负荷，180kW，可全部切除"""
        gs = state.load_group_state["group3"]
        assert gs["name"] == "三级(一般负荷)"
        assert gs["rated_power_kw"] == 180.0
        assert gs["max_shed_ratio"] == 1.0
        assert gs["shed_priority"] == 3
        assert gs["restore_priority"] == 3


class Test02_GroupReportAndAggregate:
    """测试2：分群上报与总负荷汇总"""

    def test_single_group_report(self, state):
        """单群组上报"""
        state.report_load_group("group1", 48.5)
        gs = state.load_group_state["group1"]
        assert gs["reported_power_kw"] == 48.5
        assert gs["last_report_time"] is not None
        assert "group1" in state.load_group_reports

    def test_invalid_group_report_raises(self, state):
        """无效群组ID应报错"""
        with pytest.raises(ValueError, match="未知的负荷群组"):
            state.report_load_group("group999", 100)

    def test_group_report_updates_total_load(self, state):
        """分群上报后总负荷应自动汇总"""
        state.report_load_group("group1", 50)
        state.report_load_group("group2", 100)
        state.report_load_group("group3", 150)
        assert state.get_load_kw() == pytest.approx(300.0)

    def test_batch_report_via_load_report(self, state):
        """通过 LoadReport 批量上报群组"""
        now = datetime.now()
        gr1 = LoadGroupReport("group1", 45, now)
        gr2 = LoadGroupReport("group2", 110, now)
        gr3 = LoadGroupReport("group3", 160, now)
        total = 45 + 110 + 160
        report = LoadReport(
            load_kw=total,
            timestamp=now,
            group_reports={"group1": gr1, "group2": gr2, "group3": gr3},
        )
        state.report_load(report)
        assert state.get_load_kw() == pytest.approx(total)
        assert state.load_group_state["group2"]["reported_power_kw"] == 110
        assert state.all_groups_reported() is True


class Test03_PriorityLoadShedding:
    """测试3：优先级甩负荷（三级→二级，一级不切）"""

    def test_shed_small_gap_only_group3(self, state):
        """小缺口50kW：只从三级切，完全够"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        gap = 50
        shed, remaining = state.compute_priority_load_shedding(gap, now, "DISP-TEST-001")
        assert remaining < 0.01
        assert "group3" in shed
        assert shed["group3"] == pytest.approx(50)
        assert "group1" not in shed
        assert "group2" not in shed
        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(50)
        assert gs3["current_served_kw"] == pytest.approx(130)

    def test_shed_entire_group3_then_group2(self, state):
        """大缺口220kW：先切三级180kW，再从二级切40kW（二级最多可切72kW=120*60%）"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        gap = 220
        shed, remaining = state.compute_priority_load_shedding(gap, now, "DISP-TEST-002")
        total_shed = sum(shed.values()) + remaining
        assert "group3" in shed
        assert shed["group3"] == pytest.approx(180)
        assert "group2" in shed
        assert shed["group2"] == pytest.approx(40)
        assert "group1" not in shed
        assert remaining < 0.01
        gs1 = state.load_group_state["group1"]
        gs2 = state.load_group_state["group2"]
        gs3 = state.load_group_state["group3"]
        assert gs1["current_shed_kw"] == pytest.approx(0)
        assert gs1["current_served_kw"] == pytest.approx(50)
        assert gs2["current_shed_kw"] == pytest.approx(40)
        assert gs2["current_served_kw"] == pytest.approx(80)
        assert gs3["current_shed_kw"] == pytest.approx(180)
        assert gs3["current_served_kw"] == pytest.approx(0)

    def test_shed_group2_max_limit_respected(self, state):
        """二级最大切除比例不超过60%：切完三级180+二级72=252后，仍有缺口则无法再切"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        gap = 300
        shed, remaining = state.compute_priority_load_shedding(gap, now, "DISP-TEST-003")
        assert shed["group3"] == pytest.approx(180)
        assert shed["group2"] == pytest.approx(72)
        assert "group1" not in shed
        assert remaining == pytest.approx(48)
        gs1 = state.load_group_state["group1"]
        assert gs1["current_shed_kw"] == pytest.approx(0)

    def test_group1_never_shed_any_gap(self, state):
        """任何情况下一级负荷都不切"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        gap = 1000
        shed, remaining = state.compute_priority_load_shedding(gap, now, "DISP-TEST-004")
        gs1 = state.load_group_state["group1"]
        assert gs1["current_shed_kw"] == pytest.approx(0)
        assert gs1["current_served_kw"] == pytest.approx(50)
        assert "group1" not in shed


class Test04_RestorePowerSupply:
    """测试4：恢复供电（一级→二级→三级）"""

    def test_restore_by_priority(self, state):
        """先切三级100+二级50；恢复时按恢复优先级，先恢复二级再恢复三级"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        gap = 150
        shed, _ = state.compute_priority_load_shedding(gap, now, "DISP-TEST-SHED")
        assert shed["group3"] == pytest.approx(150)

        now2 = now + timedelta(minutes=1)
        restored, leftover = state.restore_load_groups(80, now2, "DISP-TEST-RESTORE1")

        assert len(restored) == 1
        first_restored_gid = list(restored.keys())[0]
        first_kw = list(restored.values())[0]
        assert first_restored_gid == "group2" or first_restored_gid == "group3"

    def test_restore_priority_order_group1_first(self, state):
        """恢复优先级：如果有多个group在shed，恢复顺序1→2→3"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()

        state.load_group_state["group1"]["current_shed_kw"] = 0
        state.load_group_state["group2"]["current_shed_kw"] = 72
        state.load_group_state["group3"]["current_shed_kw"] = 180

        now2 = now + timedelta(minutes=1)
        restored, leftover = state.restore_load_groups(100, now2, "DISP-TEST-RESTORE2")

        gids_in_order = list(restored.keys())
        if "group2" in restored and "group3" in restored:
            idx_g2 = gids_in_order.index("group2")
            idx_g3 = gids_in_order.index("group3")
            assert idx_g2 < idx_g3

    def test_fully_restored_clears_active_event(self, state):
        """完全恢复后，活跃切除事件应被清除"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        state.compute_priority_load_shedding(80, now, "DISP-TEST-005")
        assert "group3" in state._active_shed_events

        now2 = now + timedelta(minutes=1)
        state.restore_load_groups(200, now2, "DISP-TEST-RESTORE3")
        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] < 0.01
        assert "group3" not in state._active_shed_events


class Test05_GroupStatusQuery:
    """测试5：群组状态查询"""

    def test_status_normal(self, state):
        """正常状态：正常供电"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        all_status = state.get_load_group_status()
        assert all_status["group1"]["supply_status"] == "正常"
        assert all_status["group2"]["supply_status"] == "正常"
        assert all_status["group3"]["supply_status"] == "正常"

    def test_status_partial_shed(self, state):
        """部分切除：部分切除"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(60, datetime.now(), "D-TEST")
        s3 = state.get_load_group_status("group3")
        assert s3["supply_status"] == "部分切除"
        assert s3["current_shed_kw"] == pytest.approx(60)
        assert s3["is_actively_shed"] is True

    def test_status_fully_shed(self, state):
        """完全切除：完全切除"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(200, datetime.now(), "D-TEST2")
        s3 = state.get_load_group_status("group3")
        assert s3["supply_status"] == "完全切除"
        assert s3["current_shed_kw"] == pytest.approx(180)

    def test_status_group1_always_normal(self, state):
        """一级永远是正常状态"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(500, datetime.now(), "D-TEST3")
        s1 = state.get_load_group_status("group1")
        assert s1["supply_status"] == "正常"
        assert s1["current_shed_kw"] == pytest.approx(0)


class Test06_GroupConfigUpdate:
    """测试6：群组配置修改"""

    def test_update_rated_power(self, state):
        """修改额定功率"""
        success = state.update_load_group_config("group2", rated_power_kw=150.0)
        assert success is True
        assert state.load_group_state["group2"]["rated_power_kw"] == 150.0
        assert config.LOAD_GROUP_CONFIG["group2"]["rated_power_kw"] == 150.0

    def test_update_max_shed_ratio(self, state):
        """修改二级最大切除比例"""
        success = state.update_load_group_config("group2", max_shed_ratio=0.7)
        assert success is True
        assert state.load_group_state["group2"]["max_shed_ratio"] == 0.7
        assert config.LOAD_GROUP_CONFIG["group2"]["max_shed_ratio"] == 0.7

    def test_group1_max_shed_ratio_rejected(self, state):
        """一级不允许设置max_shed_ratio>0"""
        success = state.update_load_group_config("group1", max_shed_ratio=0.1)
        assert success is False
        assert state.load_group_state["group1"]["max_shed_ratio"] == 0.0

    def test_invalid_group_update_rejected(self, state):
        """无效群组修改失败"""
        success = state.update_load_group_config("group999", rated_power_kw=999)
        assert success is False

    def test_negative_rated_power_rejected(self, state):
        """额定功率不能为负"""
        success = state.update_load_group_config("group2", rated_power_kw=-10)
        assert success is False

    def test_config_change_affects_next_shed_decision(self, state):
        """配置修改后立刻影响下次调度决策：二级max_shed_ratio改为1.0后，可以全切"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.update_load_group_config("group2", max_shed_ratio=1.0)
        shed, remaining = state.compute_priority_load_shedding(300, datetime.now(), "D-CFGTEST")
        total = sum(shed.values())
        assert "group2" in shed
        assert shed["group2"] == pytest.approx(120)
        assert remaining == pytest.approx(0)


class Test07_ShedEventHistory:
    """测试7：切除事件历史"""

    def test_event_created_on_shed(self, state):
        """切除时创建事件"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        before = len(state.load_group_shed_events)
        state.compute_priority_load_shedding(50, datetime.now(), "D-EVENT-01")
        after = len(state.load_group_shed_events)
        assert after > before
        g3_event = [e for e in state.load_group_shed_events if e.group_id == "group3"]
        assert len(g3_event) > 0
        assert g3_event[-1].shed_power_kw == pytest.approx(50)

    def test_event_closed_on_restore(self, state):
        """完全恢复时事件被关闭（设置ended_at和duration）"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(100, now, "D-EVENT-02")
        event = [e for e in state.load_group_shed_events if e.group_id == "group3"][-1]
        assert event.ended_at is None

        now2 = now + timedelta(minutes=30)
        state.restore_load_groups(500, now2, "D-RESTORE-EVENT")
        assert event.ended_at is not None
        assert event.duration_minutes == pytest.approx(30.0)

    def test_history_query_all(self, state):
        """查询全部历史事件"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(50, now, "D-1")
        state.compute_priority_load_shedding(200, now + timedelta(minutes=1), "D-2")
        events = state.get_load_group_shed_history(limit=10)
        assert len(events) >= 2

    def test_history_query_filter_by_group(self, state):
        """按群组过滤历史事件"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(250, now, "D-FILTER")
        events_g2 = state.get_load_group_shed_history(group_id="group2", limit=10)
        assert all(e["group_id"] == "group2" for e in events_g2)
        assert len(events_g2) >= 1


class Test08_ReliabilityStats:
    """测试8：可靠性统计"""

    def test_snapshot_recorded(self, state):
        """每次调度记录可靠性快照"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        now = datetime.now()
        state.record_reliability_snapshot(now)
        for gid in ["group1", "group2", "group3"]:
            assert len(state.load_group_reliability_history[gid]) == 1

    def test_reliability_100pct_when_normal(self, state):
        """正常时段可靠性为100%"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        for i in range(10):
            state.record_reliability_snapshot(datetime.now() + timedelta(minutes=i))
        stats = state.get_load_group_reliability_stats()
        assert stats["group1"]["reliability_percent"] == pytest.approx(100.0)
        assert stats["group2"]["reliability_percent"] == pytest.approx(100.0)
        assert stats["group3"]["reliability_percent"] == pytest.approx(100.0)

    def test_reliability_drops_when_shed(self, state):
        """甩负荷时段可靠性降低"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.compute_priority_load_shedding(100, datetime.now(), "D-REL-01")
        for i in range(10):
            state.record_reliability_snapshot(datetime.now() + timedelta(minutes=i))
        stats = state.get_load_group_reliability_stats("group3")
        assert stats["reliability_percent"] == pytest.approx(0.0)
        assert stats["max_shed_power_kw"] > 0


class Test09_FullDispatchIntegration:
    """测试9：完整调度流程集成测试"""

    def test_full_dispatch_normal_no_shed(self, engine, state):
        """场景：新能源充足，正常供电，无甩负荷"""
        now = datetime(2026, 6, 19, 14, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=40, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=150, now=now)
        total_load = 50 + 120 + 150

        decision = engine.execute(now=now)

        assert decision.load_shed_kw < 0.01
        assert decision.load_served_kw == pytest.approx(total_load)
        gs1 = state.get_load_group_status("group1")
        gs2 = state.get_load_group_status("group2")
        gs3 = state.get_load_group_status("group3")
        assert gs1["supply_status"] == "正常"
        assert gs2["supply_status"] == "正常"
        assert gs3["supply_status"] == "正常"

    def test_full_dispatch_shortage_priority_shed(self, engine, state):
        """场景：供电严重不足，按优先级甩负荷"""
        now = datetime(2026, 6, 19, 2, 0, 0)
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now)
        state.bess_state["bes1"].soc = 0.2
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)

        decision = engine.execute(now=now)

        gs3 = state.get_load_group_status("group3")
        gs2 = state.get_load_group_status("group2")
        gs1 = state.get_load_group_status("group1")

        assert gs1["current_shed_kw"] == pytest.approx(0)
        assert gs1["supply_status"] == "正常"
        if decision.load_shed_kw > 0:
            assert gs3["current_shed_kw"] >= gs2["current_shed_kw"]
            assert len(decision.group_shed_details) > 0

    def test_full_dispatch_restore_on_surplus(self, engine, state):
        """场景：先甩负荷，下一轮供电充足时恢复"""
        now1 = datetime(2026, 6, 19, 2, 0, 0)
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now1)
        state.bess_state["bes1"].soc = 0.2
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now1)
        d1 = engine.execute(now=now1)

        shed_occurred = d1.load_shed_kw > 0.01

        now2 = now1 + timedelta(minutes=1)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now2)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now2)
        d2 = engine.execute(now=now2)

        gs3_after = state.get_load_group_status("group3")
        gs2_after = state.get_load_group_status("group2")

        if shed_occurred and d2.load_shed_kw < 0.01:
            assert len(d2.group_restore_details) > 0
            assert gs3_after["current_shed_kw"] < 0.01 or gs2_after["current_shed_kw"] < 72

    def test_dispatch_includes_group_details_in_result(self, engine, state):
        """决策结果中包含群组详情"""
        now = datetime(2026, 6, 19, 2, 0, 0)
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now)
        state.bess_state["bes1"].soc = 0.2
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        decision = engine.execute(now=now)

        assert hasattr(decision, "group_shed_details")
        assert hasattr(decision, "group_restore_details")
        assert isinstance(decision.group_shed_details, dict)
        assert isinstance(decision.group_restore_details, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
