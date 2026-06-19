"""
单元测试：动态限额与自适应保护策略
测试：
  1. 供电压力指数计算
  2. 模式判定（宽松/正常/紧急）
  3. 动态切除比例调整
  4. 紧急模式强制切除
  5. 模式切换与逐步恢复
  6. 查询接口
  7. 手动锁定模式
  8. 调度集成测试
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


class Test01_PowerPressureIndex:
    """测试1：供电压力指数计算"""

    def test_pressure_index_low_soc(self, state):
        """电池SOC低于30%加30分"""
        now = datetime(2026, 6, 19, 10, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.25

        score = state.compute_power_pressure_index(now)

        assert score >= 30
        assert state.power_pressure_index == score

    def test_pressure_index_low_renewable(self, state):
        """新能源出力低于总负荷50%加20分"""
        now = datetime(2026, 6, 19, 10, 0, 0)
        _report_all_sources(state, pv1=30, pv2=20, wt1=10, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.8

        score = state.compute_power_pressure_index(now)

        assert score >= 20

    def test_pressure_index_diesel_running(self, state):
        """柴油机正在运行加15分"""
        now = datetime(2026, 6, 19, 10, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.8
        state.start_diesel("ds1", now)

        score = state.compute_power_pressure_index(now)

        assert score >= 15

    def test_pressure_index_peak_hour(self, state):
        """峰时段加10分"""
        now = datetime(2026, 6, 19, 12, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.8

        score = state.compute_power_pressure_index(now)

        assert score >= 10

    def test_pressure_index_cap_at_100(self, state):
        """压力指数最高不超过100"""
        now = datetime(2026, 6, 19, 12, 0, 0)
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.1
        state.start_diesel("ds1", now)

        for i in range(5):
            state.dispatch_history.append(DispatchDecision(
                timestamp=now + timedelta(minutes=i),
                pv_output={},
                wt_output={},
                diesel_output={},
                bess_action={},
                grid_import_kw=0,
                grid_export_kw=0,
                load_served_kw=100,
                load_shed_kw=50,
                cost=0,
                tariff_period="flat",
                grid_buy_price=0.8,
            ))

        score = state.compute_power_pressure_index(now)

        assert score <= 100.01
        assert score >= 99.99

    def test_pressure_history_recorded(self, state):
        """压力指数历史被记录"""
        now = datetime(2026, 6, 19, 10, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.8

        for i in range(5):
            state.compute_power_pressure_index(now + timedelta(minutes=i))

        history = state.get_power_pressure_history()
        assert len(history) >= 5

    def test_pressure_history_max_size(self, state):
        """压力指数历史最多保留50条"""
        now = datetime(2026, 6, 19, 10, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.8

        for i in range(100):
            state.compute_power_pressure_index(now + timedelta(minutes=i))

        assert len(state.power_pressure_history) <= config.DYNAMIC_SHED_CONFIG["pressure_history_size"]


class Test02_ShedModeDetermination:
    """测试2：模式判定"""

    def test_relaxed_mode_below_30(self, state):
        """压力指数低于30为宽松模式"""
        mode = state.determine_shed_mode(20)
        assert mode == "relaxed"

    def test_normal_mode_30_to_70(self, state):
        """压力指数30到70为正常模式"""
        mode = state.determine_shed_mode(50)
        assert mode == "normal"

    def test_emergency_mode_above_70(self, state):
        """压力指数高于70为紧急模式"""
        mode = state.determine_shed_mode(80)
        assert mode == "emergency"

    def test_boundary_30_is_normal(self, state):
        """边界值30为正常模式"""
        mode = state.determine_shed_mode(30)
        assert mode == "normal"

    def test_boundary_70_is_emergency(self, state):
        """边界值70为紧急模式"""
        mode = state.determine_shed_mode(70)
        assert mode == "emergency"


class Test03_DynamicShedRatio:
    """测试3：动态切除比例调整"""

    def test_relaxed_mode_group3_half_ratio(self, state):
        """宽松模式：三级群组最大切除比例降为配置值的50%"""
        state.current_shed_mode = "relaxed"
        ratio = state.get_dynamic_max_shed_ratio("group3")
        base_ratio = config.LOAD_GROUP_CONFIG["group3"]["max_shed_ratio"]
        assert ratio == pytest.approx(base_ratio * 0.5)

    def test_relaxed_mode_group2_no_shed(self, state):
        """宽松模式：二级群组不允许切除"""
        state.current_shed_mode = "relaxed"
        ratio = state.get_dynamic_max_shed_ratio("group2")
        assert ratio == pytest.approx(0.0)

    def test_normal_mode_config_ratio(self, state):
        """正常模式：按配置值执行"""
        state.current_shed_mode = "normal"
        ratio_g3 = state.get_dynamic_max_shed_ratio("group3")
        ratio_g2 = state.get_dynamic_max_shed_ratio("group2")
        base_g3 = config.LOAD_GROUP_CONFIG["group3"]["max_shed_ratio"]
        base_g2 = config.LOAD_GROUP_CONFIG["group2"]["max_shed_ratio"]
        assert ratio_g3 == pytest.approx(base_g3)
        assert ratio_g2 == pytest.approx(base_g2)

    def test_emergency_mode_group3_full_shed(self, state):
        """紧急模式：三级群组允许全切"""
        state.current_shed_mode = "emergency"
        ratio = state.get_dynamic_max_shed_ratio("group3")
        assert ratio == pytest.approx(1.0)

    def test_emergency_mode_group2_increased(self, state):
        """紧急模式：二级群组切除比例上浮到配置值的150%（但不超过100%）"""
        state.current_shed_mode = "emergency"
        ratio = state.get_dynamic_max_shed_ratio("group2")
        base_ratio = config.LOAD_GROUP_CONFIG["group2"]["max_shed_ratio"]
        expected = min(1.0, base_ratio * 1.5)
        assert ratio == pytest.approx(expected)

    def test_emergency_mode_group2_capped_at_100(self, state):
        """紧急模式：二级群组最多不超过100%"""
        state.current_shed_mode = "emergency"
        ratio = state.get_dynamic_max_shed_ratio("group2")
        assert ratio <= 1.0

    def test_group1_always_zero(self, state):
        """一级群组任何模式都不允许切除"""
        for mode in ["relaxed", "normal", "emergency"]:
            state.current_shed_mode = mode
            ratio = state.get_dynamic_max_shed_ratio("group1")
            assert ratio == pytest.approx(0.0)


class Test04_DynamicLoadShedding:
    """测试4：动态甩负荷"""

    def test_relaxed_mode_limited_shed(self, state):
        """宽松模式下甩受限于50%"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.current_shed_mode = "relaxed"

        gap = 150
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-001")

        max_g3 = 180 * 0.5
        assert shed.get("group3", 0) <= max_g3 + 0.01
        assert "group2" not in shed

    def test_normal_mode_normal_shed(self, state):
        """正常模式下按配置值切除"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.current_shed_mode = "normal"

        gap = 220
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-002")

        assert shed.get("group3", 0) == pytest.approx(180)
        assert shed.get("group2", 0) == pytest.approx(40)

    def test_emergency_mode_more_shed(self, state):
        """紧急模式下可以切更多二级负荷"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.current_shed_mode = "emergency"

        gap = 280
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-003")

        max_g2 = 120 * min(1.0, 0.6 * 1.5)
        assert shed.get("group3", 0) == pytest.approx(180)
        assert shed.get("group2", 0) <= max_g2 + 0.01
        assert shed.get("group2", 0) > 72

    def test_emergency_forced_shed_low_soc(self, state):
        """紧急模式且SOC低于25%时强制全切三级（即使有缺口）"""
        now = datetime.now()
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.2
        state.current_shed_mode = "emergency"

        gap = 50
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-004")

        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(180)

    def test_emergency_forced_shed_without_gap(self, state):
        """紧急模式且SOC低于25%时，即使无供电缺口也强制全切三级"""
        now = datetime.now()
        _report_all_sources(state, pv1=300, pv2=300, wt1=100, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.2
        state.current_shed_mode = "emergency"

        gap = 0
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-GAPLESS-001")

        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(180)
        assert "group3" in shed
        assert shed["group3"] == pytest.approx(180)

    def test_emergency_soc_above_threshold_no_forced(self, state):
        """紧急模式但SOC高于25%时，无缺口就不强制切除"""
        now = datetime.now()
        _report_all_sources(state, pv1=300, pv2=300, wt1=100, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.3
        state.current_shed_mode = "emergency"

        gap = 0
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-GAPLESS-002")

        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(0)
        assert len(shed) == 0

    def test_normal_mode_no_forced_shed(self, state):
        """正常模式即使SOC很低也不强制切除"""
        now = datetime.now()
        _report_all_sources(state, pv1=300, pv2=300, wt1=100, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.1
        state.current_shed_mode = "normal"

        gap = 0
        shed, remaining = state.compute_priority_load_shedding_dynamic(gap, now, "TEST-GAPLESS-003")

        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(0)
        assert len(shed) == 0


class Test05_EmergencyRestore:
    """测试5：紧急模式恢复（逐步恢复）"""

    def test_emergency_restore_gradual(self, state):
        """从紧急模式降回正常模式时，逐步恢复（每轮最多恢复总切除量的30%）"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)

        state.current_shed_mode = "emergency"
        state.compute_priority_load_shedding_dynamic(250, now, "SHED-001")

        assert state.emergency_shed_extra_kw.get("group3", 0) > 0 or state.emergency_shed_extra_kw.get("group2", 0) > 0

        total_extra_before = sum(state.emergency_shed_extra_kw.values())
        assert total_extra_before > 0

        state.current_shed_mode = "normal"
        state.pending_restore_from_emergency = True

        now2 = now + timedelta(minutes=1)
        restored, leftover = state.restore_load_groups_dynamic(500, now2, "RESTORE-001")

        total_restored = sum(restored.values())
        max_restore_expected = total_extra_before * 0.3

        assert total_restored <= max_restore_expected + 0.01
        assert total_restored > 0

    def test_multiple_cycles_restore_all(self, state):
        """多轮调度后完全恢复"""
        now = datetime.now()
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)

        state.current_shed_mode = "emergency"
        state.compute_priority_load_shedding_dynamic(250, now, "SHED-002")

        state.current_shed_mode = "normal"
        state.pending_restore_from_emergency = True

        total_extra = sum(state.emergency_shed_extra_kw.values())
        assert total_extra > 0
        cycles = 0
        max_cycles = 50

        while state.pending_restore_from_emergency and cycles < max_cycles:
            cycles += 1
            now += timedelta(minutes=1)
            state.restore_load_groups_dynamic(500, now, f"RESTORE-{cycles}")

        assert cycles < max_cycles
        assert not state.pending_restore_from_emergency
        assert sum(state.emergency_shed_extra_kw.values()) < 0.01


class Test06_QueryInterfaces:
    """测试6：查询接口"""

    def test_get_power_pressure_info(self, state):
        """查询当前供电压力指数和模式"""
        info = state.get_power_pressure_info()
        assert "current_pressure_index" in info
        assert "current_mode" in info
        assert "current_mode_chinese" in info
        assert "manual_lock" in info

    def test_get_power_pressure_history(self, state):
        """查询压力指数历史趋势"""
        now = datetime.now()
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)
        state.bess_state["bes1"].soc = 0.8

        for i in range(10):
            state.compute_power_pressure_index(now + timedelta(minutes=i))

        history = state.get_power_pressure_history(limit=5)
        assert len(history) == 5
        assert "timestamp" in history[0]
        assert "pressure_index" in history[0]

    def test_get_dynamic_shed_limits(self, state):
        """查询各群组当前动态限额"""
        _report_load_groups(state, g1=50, g2=120, g3=180)
        state.current_shed_mode = "normal"

        limits = state.get_dynamic_shed_limits()
        assert "group1" in limits
        assert "group2" in limits
        assert "group3" in limits

        for gid in ["group1", "group2", "group3"]:
            assert "configured_max_shed_ratio" in limits[gid]
            assert "dynamic_max_shed_ratio" in limits[gid]
            assert "configured_max_shed_kw" in limits[gid]
            assert "dynamic_max_shed_kw" in limits[gid]

    def test_get_shed_mode_history(self, state):
        """查询模式切换历史记录"""
        now = datetime.now()
        _report_all_sources(state, pv1=100, pv2=100, wt1=50, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=80, now=now)

        state.current_shed_mode = "normal"

        state.update_shed_mode(now, 20)
        state.update_shed_mode(now + timedelta(minutes=1), 80)
        state.update_shed_mode(now + timedelta(minutes=2), 50)

        history = state.get_shed_mode_history()
        assert len(history) >= 2
        assert "old_mode" in history[0]
        assert "new_mode" in history[0]
        assert "reason" in history[0]
        assert "trigger" in history[0]


class Test07_ManualLock:
    """测试7：手动锁定模式"""

    def test_manual_lock_relaxed(self, state):
        """手动锁定到宽松模式"""
        state.current_shed_mode = "normal"
        result = state.set_shed_mode_manual_lock(True, "relaxed")
        assert result is True
        assert state.shed_mode_manual_lock is True
        assert state.current_shed_mode == "relaxed"

    def test_manual_lock_emergency(self, state):
        """手动锁定到紧急模式"""
        state.current_shed_mode = "normal"
        result = state.set_shed_mode_manual_lock(True, "emergency")
        assert result is True
        assert state.current_shed_mode == "emergency"

    def test_manual_unlock(self, state):
        """手动解锁"""
        state.set_shed_mode_manual_lock(True, "relaxed")
        state.set_shed_mode_manual_lock(False)
        assert state.shed_mode_manual_lock is False
        assert state.shed_mode_manual_mode is None

    def test_manual_lock_invalid_mode(self, state):
        """无效模式锁定失败"""
        result = state.set_shed_mode_manual_lock(True, "invalid_mode")
        assert result is False

    def test_manual_lock_prevents_auto_switch(self, state):
        """锁定后自动切换不再生效"""
        now = datetime.now()
        state.set_shed_mode_manual_lock(True, "relaxed")

        new_mode, changed, _ = state.update_shed_mode(now, 90)
        assert changed is False
        assert new_mode == "relaxed"


class Test08_FullDispatchIntegration:
    """测试8：完整调度流程集成"""

    def test_dispatch_includes_pressure_info(self, engine, state):
        """调度结果中包含压力指数和模式信息"""
        now = datetime(2026, 6, 19, 14, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=40, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=150, now=now)

        decision = engine.execute(now=now)

        pressure_info = state.get_power_pressure_info()
        assert pressure_info["current_pressure_index"] >= 0
        assert pressure_info["current_mode"] in ["relaxed", "normal", "emergency"]

        has_pressure_note = any("供电压力指数" in note for note in decision.notes)
        assert has_pressure_note

    def test_emergency_mode_in_dispatch(self, engine, state):
        """紧急模式下调度切除更多负荷"""
        now = datetime(2026, 6, 19, 2, 0, 0)
        _report_all_sources(state, pv1=0, pv2=0, wt1=0, now=now)
        state.bess_state["bes1"].soc = 0.2
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)

        state.set_shed_mode_manual_lock(True, "emergency")
        decision_emergency = engine.execute(now=now)

        gs2_emergency = state.load_group_state["group2"]["current_shed_kw"]

        state.set_shed_mode_manual_lock(False)
        state.load_group_state["group2"]["current_shed_kw"] = 0
        state.load_group_state["group3"]["current_shed_kw"] = 0
        state._active_shed_events = {}

        state.set_shed_mode_manual_lock(True, "normal")
        decision_normal = engine.execute(now=now + timedelta(minutes=1))

        gs2_normal = state.load_group_state["group2"]["current_shed_kw"]

        assert gs2_emergency >= gs2_normal

    def test_dynamic_limits_query_integration(self, engine, state):
        """集成测试：查询动态限额"""
        now = datetime(2026, 6, 19, 14, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=40, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=150, now=now)

        engine.execute(now=now)

        limits = state.get_dynamic_shed_limits()
        assert len(limits) == 3
        for gid in limits:
            assert limits[gid]["dynamic_max_shed_ratio"] >= 0
            assert limits[gid]["dynamic_max_shed_ratio"] <= 1

    def test_dispatch_emergency_forced_shed_no_gap(self, engine, state):
        """集成测试：调度流程中紧急模式+低SOC无缺口时也强制切三级"""
        now = datetime(2026, 6, 19, 14, 0, 0)
        _report_all_sources(state, pv1=200, pv2=200, wt1=100, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=180, now=now)
        state.bess_state["bes1"].soc = 0.2
        state.set_shed_mode_manual_lock(True, "emergency")

        decision = engine.execute(now=now)

        gs3 = state.load_group_state["group3"]
        assert gs3["current_shed_kw"] == pytest.approx(180)
        assert decision.load_shed_kw >= 179.99

        has_forced_note = any("紧急保护" in note and "强制切除" in note for note in decision.notes)
        assert has_forced_note

    def test_dispatch_api_endpoints_exist(self, engine, state):
        """集成测试：验证所有查询接口返回正常格式"""
        now = datetime(2026, 6, 19, 14, 0, 0)
        _report_all_sources(state, pv1=100, pv2=100, wt1=40, now=now)
        _report_load_groups(state, g1=50, g2=120, g3=150, now=now)

        engine.execute(now=now)

        pressure_info = state.get_power_pressure_info()
        assert "current_pressure_index" in pressure_info
        assert "current_mode" in pressure_info
        assert "manual_lock" in pressure_info

        pressure_history = state.get_power_pressure_history(limit=10)
        assert isinstance(pressure_history, list)

        limits = state.get_dynamic_shed_limits()
        assert len(limits) == 3
        for gid in ["group1", "group2", "group3"]:
            assert gid in limits

        mode_history = state.get_shed_mode_history(limit=10)
        assert isinstance(mode_history, list)

        restore_status = state.get_emergency_restore_status()
        assert "pending_restore" in restore_status
        assert "total_extra_shed_kw" in restore_status


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
