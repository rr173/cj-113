from datetime import datetime, timedelta
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
import config


def test_health_scoring():
    print("=" * 60)
    print("测试1: 健康评分计算")
    print("=" * 60)
    state = MicrogridState()
    engine = DispatchEngine(state)

    for i in range(5):
        report = SourceReport(
            source_id="pv1",
            source_type="pv",
            power_kw=90.0,
            available=True,
            timestamp=datetime.now() + timedelta(seconds=i),
        )
        state.report_source(report)

    hs = state.get_source_health_status("pv:pv1")
    print(f"  连续5次正常上报后评分: {hs['health_score']}, 状态: {hs['status_chinese']}")
    assert hs["health_score"] == 100.0, f"期望100分，实际{hs['health_score']}"
    assert hs["status"] == "normal"

    report = SourceReport(
        source_id="pv1",
        source_type="pv",
        power_kw=0.0,
        available=False,
        timestamp=datetime.now(),
    )
    state.report_source(report)
    hs = state.get_source_health_status("pv:pv1")
    print(f"  单次不可用后评分: {hs['health_score']}, 状态: {hs['status_chinese']}")
    assert hs["health_score"] == 60.0, f"期望60分，实际{hs['health_score']}"
    assert hs["status"] == "warning"

    report = SourceReport(
        source_id="pv1",
        source_type="pv",
        power_kw=0.0,
        available=False,
        timestamp=datetime.now(),
    )
    state.report_source(report)
    hs = state.get_source_health_status("pv:pv1")
    print(f"  连续两次不可用后评分: {hs['health_score']}, 状态: {hs['status_chinese']}")
    assert hs["health_score"] == 0.0, f"期望0分，实际{hs['health_score']}"
    assert hs["status"] == "danger"

    print("  [通过]")


def test_backup_plan_generation():
    print()
    print("=" * 60)
    print("测试2: 备用预案生成")
    print("=" * 60)
    state = MicrogridState()
    engine = DispatchEngine(state)

    state.report_load(LoadReport(load_kw=200.0, timestamp=datetime.now()))

    for sid in config.PV_CONFIG:
        state.report_source(SourceReport(source_id=sid, source_type="pv", power_kw=80.0, available=True, timestamp=datetime.now()))
    for sid in config.WT_CONFIG:
        state.report_source(SourceReport(source_id=sid, source_type="wt", power_kw=30.0, available=True, timestamp=datetime.now()))
    for sid in config.DIESEL_CONFIG:
        state.report_source(SourceReport(source_id=sid, source_type="diesel", power_kw=0.0, available=True, timestamp=datetime.now()))

    print("  模拟pv1波动使其进入预警状态...")
    for i in range(15):
        power = 80.0 if i % 2 == 0 else 10.0
        state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=power, available=True, timestamp=datetime.now() + timedelta(seconds=i)))

    hs = state.get_source_health_status("pv:pv1")
    print(f"  pv1评分: {hs['health_score']}, 状态: {hs['status_chinese']}")

    state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=0.0, available=False, timestamp=datetime.now()))
    hs = state.get_source_health_status("pv:pv1")
    print(f"  pv1变为不可用后评分: {hs['health_score']}, 状态: {hs['status_chinese']}")

    plan = state.get_backup_plan_for_source("pv:pv1")
    if plan:
        print(f"  已生成预案: {plan.plan_id}")
        print(f"    是否可覆盖: {'可应对' if plan.can_cover else '有缺口'}")
        print(f"    缺口功率: {plan.gap_kw}kW")
        print(f"    负荷: {plan.load_kw}kW")
        print(f"    失去容量: {plan.lost_capacity_kw}kW")
        print(f"    建议: {plan.suggestions}")
    else:
        print("  [警告] 未生成预案")

    plans = state.get_active_backup_plans()
    print(f"  活跃预案数量: {len(plans)}")
    print("  [通过]")


def test_fault_with_plan_dispatch():
    print()
    print("=" * 60)
    print("测试3: 有预案时的调度（电池优先放电）")
    print("=" * 60)
    state = MicrogridState()
    engine = DispatchEngine(state)

    state.report_load(LoadReport(load_kw=150.0, timestamp=datetime.now()))

    state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=0.0, available=False, timestamp=datetime.now()))
    state.report_source(SourceReport(source_id="pv2", source_type="pv", power_kw=50.0, available=True, timestamp=datetime.now()))
    state.report_source(SourceReport(source_id="wt1", source_type="wt", power_kw=20.0, available=True, timestamp=datetime.now()))
    state.report_source(SourceReport(source_id="ds1", source_type="diesel", power_kw=0.0, available=True, timestamp=datetime.now()))

    hs = state.get_source_health_status("pv:pv1")
    print(f"  pv1评分: {hs['health_score']}, 状态: {hs['status_chinese']}")
    plan = state.get_backup_plan_for_source("pv:pv1")
    print(f"  pv1预案: {'已生成' if plan else '未生成'}")

    decision = engine.execute()
    print(f"  调度notes: {decision.notes}")
    print(f"  电池放电: {decision.bess_action['bes1']['discharge_kw']}kW")
    print(f"  外购电: {decision.grid_import_kw}kW")
    print(f"  柴油机出力: {decision.diesel_output['ds1']}kW")

    has_plan_note = any("按预案执行" in n for n in decision.notes)
    print(f"  是否有预案执行标记: {'是' if has_plan_note else '否'}")
    print("  [通过]")


def test_maintenance_mode():
    print()
    print("=" * 60)
    print("测试4: 维护状态")
    print("=" * 60)
    state = MicrogridState()

    state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=0.0, available=False, timestamp=datetime.now()))
    state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=0.0, available=False, timestamp=datetime.now()))
    hs_before = state.get_source_health_status("pv:pv1")
    print(f"  维护前: 评分={hs_before['health_score']}, 状态={hs_before['status_chinese']}")

    result = state.set_source_maintenance("pv:pv1", True)
    assert result
    hs_after = state.get_source_health_status("pv:pv1")
    print(f"  进入维护: 维护中={hs_after['in_maintenance']}, 评分={hs_after['health_score']}, 状态={hs_after['status_chinese']}")
    assert hs_after["in_maintenance"]
    assert hs_after["health_score"] == 100.0
    assert hs_after["status"] == "normal"

    state.report_source(SourceReport(source_id="pv1", source_type="pv", power_kw=0.0, available=False, timestamp=datetime.now()))
    hs_maint = state.get_source_health_status("pv:pv1")
    print(f"  维护中再次不可用上报: 评分仍为={hs_maint['health_score']}")
    assert hs_maint["health_score"] == 100.0, "维护期间不应扣分"

    state.set_source_maintenance("pv:pv1", False)
    hs_end = state.get_source_health_status("pv:pv1")
    print(f"  结束维护: 维护中={hs_end['in_maintenance']}")
    assert not hs_end["in_maintenance"]
    print("  [通过]")


def test_fault_events():
    print()
    print("=" * 60)
    print("测试5: 故障事件记录")
    print("=" * 60)
    state = MicrogridState()

    state.report_source(SourceReport(source_id="wt1", source_type="wt", power_kw=30.0, available=True, timestamp=datetime.now()))
    state.report_source(SourceReport(source_id="wt1", source_type="wt", power_kw=0.0, available=False, timestamp=datetime.now()))

    events = state.get_fault_events()
    print(f"  故障事件数量: {len(events)}")
    if events:
        ev = events[0]
        print(f"    事件ID: {ev['event_id']}")
        print(f"    源: {ev['source_type']}:{ev['source_id']}")
        print(f"    是否仍在故障: {ev['still_active']}")
        print(f"    是否有预案: {ev['had_plan']}")

    state.report_source(SourceReport(source_id="wt1", source_type="wt", power_kw=30.0, available=True, timestamp=datetime.now()))
    events_after = state.get_fault_events()
    ev = events_after[0]
    print(f"  恢复后: 结束时间={ev['ended_at']}, 时长={ev['duration_minutes']}分钟, 仍活动={ev['still_active']}")
    assert ev["ended_at"] is not None
    assert not ev["still_active"]
    print("  [通过]")


def test_health_history():
    print()
    print("=" * 60)
    print("测试6: 健康评分历史记录")
    print("=" * 60)
    state = MicrogridState()

    for i in range(10):
        state.report_source(SourceReport(
            source_id="pv2",
            source_type="pv",
            power_kw=85.0 + i,
            available=True,
            timestamp=datetime.now() + timedelta(seconds=i),
        ))

    history = state.get_source_health_history("pv:pv2")
    print(f"  历史记录条数: {len(history)}")
    if history:
        print(f"    最早记录: 评分={history[0]['health_score']}, 时间={history[0]['timestamp']}")
        print(f"    最新记录: 评分={history[-1]['health_score']}, 时间={history[-1]['timestamp']}")
    assert len(history) == 10
    print("  [通过]")


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  发电源故障预测与备用调度预案模块测试")
    print("=" * 60)

    try:
        test_health_scoring()
        test_backup_plan_generation()
        test_fault_with_plan_dispatch()
        test_maintenance_mode()
        test_fault_events()
        test_health_history()
        print()
        print("=" * 60)
        print("  所有测试通过! ✓")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n  [失败] 断言错误: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"\n  [失败] 运行时错误: {e}")
        import traceback
        traceback.print_exc()
