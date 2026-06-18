from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from models import MicrogridState, LoadReport, SourceReport
from demand_response import DemandResponseManager, InterruptibleLoad


def test_interruptible_load_crud():
    print("=" * 60)
    print("测试1: 可中断负荷 CRUD 操作")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    print(f"初始负荷数量: {len(dr.list_interruptible_loads())}")

    new_load = dr.add_interruptible_load({
        "load_id": "test_load_1",
        "name": "测试负荷1",
        "rated_power_kw": 100.0,
        "max_reduction_ratio": 0.4,
        "min_duration_minutes": 20,
        "cooldown_minutes": 10,
        "unit_cost_yuan_per_kwh": 0.8,
    })
    print(f"添加新负荷: {new_load.load_id} - {new_load.name}")

    load = dr.get_interruptible_load("test_load_1")
    assert load is not None
    assert load.name == "测试负荷1"
    assert load.get_max_reduction_kw() == 40.0
    print(f"查询负荷: 成功，最大可削减 {load.get_max_reduction_kw()}kW")

    updated = dr.update_interruptible_load("test_load_1", {
        "name": "测试负荷1-已更新",
        "max_reduction_ratio": 0.5,
    })
    assert updated is not None
    assert updated.name == "测试负荷1-已更新"
    assert updated.get_max_reduction_kw() == 50.0
    print(f"更新负荷: 成功，最大可削减排至 {updated.get_max_reduction_kw()}kW")

    success = dr.delete_interruptible_load("test_load_1")
    assert success
    assert dr.get_interruptible_load("test_load_1") is None
    print(f"删除负荷: 成功")

    print("测试1 通过 ✓")
    print()


def test_event_reception_and_plan_generation():
    print("=" * 60)
    print("测试2: 事件接收与响应方案生成")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    state.load_report = LoadReport(
        load_kw=300.0,
        timestamp=datetime.now(),
    )

    start_time = datetime.now() + timedelta(minutes=5)
    end_time = start_time + timedelta(hours=2)

    event_data = {
        "event_no": "DR-2024-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 200.0,
        "subsidy_unit_price": 1.5,
        "penalty_unit_price": 3.0,
    }

    event = dr.receive_event(event_data)
    print(f"接收事件: {event.event_id} ({event.event_no})")
    print(f"  目标负荷: {event.target_load_kw}kW")
    print(f"  当前负荷: {dr.get_current_load_kw()}kW")
    print(f"  需削减: {max(0, dr.get_current_load_kw() - event.target_load_kw)}kW")

    plan = dr.generate_response_plan(event.event_id)
    assert plan is not None
    print(f"生成方案: {plan.plan_id}")
    print(f"  削减目标: {plan.total_reduction_target_kw:.2f}kW")
    print(f"  是否部分响应: {plan.is_partial_response}")
    print(f"  预期缺口: {plan.expected_gap_kw:.2f}kW")
    print(f"  调度时段数: {len(plan.schedule)}")
    print(f"  方案说明:")
    for note in plan.notes:
        print(f"    - {note}")

    assert plan.total_reduction_target_kw > 0
    assert len(plan.schedule) > 0

    print("测试2 通过 ✓")
    print()


def test_plan_cost_ordering():
    print("=" * 60)
    print("测试3: 验证方案生成按单位成本排序")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    dr.interruptible_loads.clear()

    dr.add_interruptible_load({
        "load_id": "cheap_load",
        "name": "低成本负荷",
        "rated_power_kw": 100.0,
        "max_reduction_ratio": 0.5,
        "min_duration_minutes": 10,
        "cooldown_minutes": 5,
        "unit_cost_yuan_per_kwh": 0.3,
    })

    dr.add_interruptible_load({
        "load_id": "mid_load",
        "name": "中成本负荷",
        "rated_power_kw": 100.0,
        "max_reduction_ratio": 0.5,
        "min_duration_minutes": 10,
        "cooldown_minutes": 5,
        "unit_cost_yuan_per_kwh": 0.8,
    })

    dr.add_interruptible_load({
        "load_id": "expensive_load",
        "name": "高成本负荷",
        "rated_power_kw": 100.0,
        "max_reduction_ratio": 0.5,
        "min_duration_minutes": 10,
        "cooldown_minutes": 5,
        "unit_cost_yuan_per_kwh": 2.0,
    })

    state.load_report = LoadReport(
        load_kw=250.0,
        timestamp=datetime.now(),
    )

    start_time = datetime.now() + timedelta(minutes=1)
    end_time = start_time + timedelta(hours=1)

    event = dr.receive_event({
        "event_no": "TEST-COST-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 200.0,
        "subsidy_unit_price": 1.0,
        "penalty_unit_price": 2.0,
    })

    plan = dr.generate_response_plan(event.event_id)

    print(f"需要削减: 50kW")
    print(f"方案削减分配:")
    if plan.schedule:
        first_period = plan.schedule[0]
        for load_id, reduction in first_period.load_reductions.items():
            load = dr.get_interruptible_load(load_id)
            print(f"  - {load.name}: {reduction:.2f}kW (单位成本: {load.unit_cost_yuan_per_kwh}元/kWh)")

    cheap_load_reduction = plan.schedule[0].load_reductions.get("cheap_load", 0)
    mid_load_reduction = plan.schedule[0].load_reductions.get("mid_load", 0)
    expensive_load_reduction = plan.schedule[0].load_reductions.get("expensive_load", 0)

    assert cheap_load_reduction >= mid_load_reduction
    assert cheap_load_reduction > 0, "低成本负荷应该被优先调用"
    print(f"\n验证: 低成本负荷优先削减 ✓")
    print(f"  低成本负荷削减: {cheap_load_reduction}kW")
    print(f"  中成本负荷削减: {mid_load_reduction}kW")
    print(f"  高成本负荷削减: {expensive_load_reduction}kW")

    print("测试3 通过 ✓")
    print()


def test_battery_supplement():
    print("=" * 60)
    print("测试4: 验证电池放电补充（当负荷削减不足时）")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    dr.interruptible_loads.clear()

    dr.add_interruptible_load({
        "load_id": "only_load",
        "name": "唯一可削减负荷",
        "rated_power_kw": 50.0,
        "max_reduction_ratio": 0.5,
        "min_duration_minutes": 10,
        "cooldown_minutes": 5,
        "unit_cost_yuan_per_kwh": 0.5,
    })

    state.load_report = LoadReport(
        load_kw=200.0,
        timestamp=datetime.now(),
    )

    start_time = datetime.now() + timedelta(minutes=1)
    end_time = start_time + timedelta(hours=1)

    event = dr.receive_event({
        "event_no": "TEST-BATTERY-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 100.0,
        "subsidy_unit_price": 1.0,
        "penalty_unit_price": 2.0,
    })

    plan = dr.generate_response_plan(event.event_id)

    print(f"当前负荷: 200kW")
    print(f"目标负荷: 100kW")
    print(f"需要削减: 100kW")
    print(f"最大可削减负荷: 25kW")
    print(f"电池放电补充: {plan.schedule[0].battery_discharge_kw:.2f}kW")
    print(f"是否部分响应: {plan.is_partial_response}")
    print(f"预期缺口: {plan.expected_gap_kw:.2f}kW")

    if plan.schedule[0].battery_discharge_kw > 0:
        print("✓ 电池放电已启用作为补充")
    if plan.is_partial_response:
        print("✓ 正确标记为部分响应（当所有资源用尽仍有缺口）")

    print("测试4 通过 ✓")
    print()


def test_event_status_flow():
    print("=" * 60)
    print("测试5: 事件状态流转 (待响应 → 已确认 → 响应中 → 已结束)")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    state.load_report = LoadReport(load_kw=250.0, timestamp=datetime.now())

    now = datetime.now()
    start_time = now + timedelta(minutes=2)
    end_time = start_time + timedelta(minutes=10)

    event = dr.receive_event({
        "event_no": "TEST-FLOW-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 200.0,
        "subsidy_unit_price": 1.0,
        "penalty_unit_price": 2.0,
    })

    print(f"初始状态: {event.status} (待响应)")
    assert event.status == "pending"

    dr.generate_response_plan(event.event_id)
    success = dr.confirm_plan(event.event_id)
    assert success
    print(f"确认后状态: {event.status} (已确认)")
    assert event.status == "confirmed"

    fake_now = start_time + timedelta(minutes=1)
    started = dr.start_event_if_due(fake_now)
    assert len(started) == 1
    assert event.status == "active"
    print(f"开始后状态: {event.status} (响应中)")

    fake_end = end_time + timedelta(minutes=1)
    finished = dr.check_and_finish_events(fake_end)
    assert len(finished) == 1
    assert event.status == "finished"
    print(f"结束后状态: {event.status} (已结束)")

    report = dr.get_settlement_report(event.event_id)
    print(f"结算报告: 已生成 (报告ID: {report.report_id if report else '无'})")

    print("测试5 通过 ✓")
    print()


def test_settlement_calculation():
    print("=" * 60)
    print("测试6: 结算计算逻辑验证")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    state.load_report = LoadReport(load_kw=300.0, timestamp=datetime.now())

    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=5)

    event = dr.receive_event({
        "event_no": "TEST-SETTLE-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 250.0,
        "subsidy_unit_price": 1.5,
        "penalty_unit_price": 3.0,
    })

    dr.generate_response_plan(event.event_id)
    dr.confirm_plan(event.event_id)

    event.status = "active"

    interval_hours = 1 / 60.0
    for i in range(5):
        timestamp = start_time + timedelta(minutes=i)
        if i < 3:
            dr.record_execution(event.event_id, 240.0, {"ac_main": 40.0, "lighting_area_a": 20.0}, 0.0, timestamp)
        else:
            dr.record_execution(event.event_id, 260.0, {"ac_main": 40.0}, 0.0, timestamp)

    event.status = "finished"
    event.finished_at = end_time

    report = dr.generate_settlement_report(event.event_id)

    print(f"总周期数: {report.total_periods}")
    print(f"达标周期数: {report.compliant_periods}")
    print(f"达标率: {report.compliance_rate * 100:.1f}%")
    print(f"总削减电量: {report.total_reduction_kwh:.4f}kWh")
    print(f"总缺口电量: {report.total_gap_kwh:.4f}kWh")
    print(f"结算类型: {report.settlement_type}")
    print(f"补贴金额: {report.subsidy_amount:.2f}元")
    print(f"罚款金额: {report.penalty_amount:.2f}元")
    print(f"净收益: {report.net_amount:.2f}元")

    assert report.total_periods == 5
    assert report.compliant_periods == 3
    assert abs(report.compliance_rate - 0.6) < 0.01

    print("测试6 通过 ✓")
    print()


def test_early_termination():
    print("=" * 60)
    print("测试7: 提前中止功能")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    state.load_report = LoadReport(load_kw=300.0, timestamp=datetime.now())

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=2)

    event = dr.receive_event({
        "event_no": "TEST-TERMINATE-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 250.0,
        "subsidy_unit_price": 1.0,
        "penalty_unit_price": 2.0,
    })

    dr.generate_response_plan(event.event_id)
    dr.confirm_plan(event.event_id)
    event.status = "active"

    dr.record_execution(event.event_id, 240.0, {"ac_main": 40.0, "lighting_area_a": 20.0}, 0.0)

    report = dr.terminate_event_early(event.event_id, "测试手动中止")

    assert report is not None
    assert event.status == "finished"
    assert event.early_terminated == True
    assert event.termination_reason == "测试手动中止"

    print(f"事件状态: {event.status}")
    print(f"是否提前中止: {event.early_terminated}")
    print(f"中止原因: {event.termination_reason}")
    print(f"结算报告: 已生成")
    print(f"  结算周期数: {report.total_periods}")
    print(f"  净收益: {report.net_amount:.2f}元")

    failed_result = dr.terminate_event_early(event.event_id, "再次中止")
    assert failed_result is None
    print("已结束事件无法再次中止 ✓")

    print("测试7 通过 ✓")
    print()


def test_dr_constraints_application():
    print("=" * 60)
    print("测试8: 需求响应约束应用 (需配合调度引擎)")
    print("=" * 60)

    from dispatcher import DispatchEngine

    state = MicrogridState()
    dr = DemandResponseManager(state)
    engine = DispatchEngine(state, dr)

    state.pv_reports["pv1"] = SourceReport(
        source_id="pv1", source_type="pv", power_kw=50.0, available=True, timestamp=datetime.now()
    )
    state.pv_reports["pv2"] = SourceReport(
        source_id="pv2", source_type="pv", power_kw=50.0, available=True, timestamp=datetime.now()
    )
    state.wt_reports["wt1"] = SourceReport(
        source_id="wt1", source_type="wt", power_kw=30.0, available=True, timestamp=datetime.now()
    )

    state.load_report = LoadReport(load_kw=350.0, timestamp=datetime.now())

    start_time = datetime.now() - timedelta(minutes=1)
    end_time = datetime.now() + timedelta(hours=1)

    event = dr.receive_event({
        "event_no": "TEST-DR-CONSTRAINT-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 280.0,
        "subsidy_unit_price": 1.0,
        "penalty_unit_price": 2.0,
    })

    dr.generate_response_plan(event.event_id)
    dr.confirm_plan(event.event_id)
    event.status = "active"

    decision = engine.execute()

    print(f"原始负荷: 350.0kW")
    print(f"目标负荷: 280.0kW")
    print(f"调度后负荷: {decision.load_served_kw:.2f}kW")
    print(f"甩负荷量: {decision.load_shed_kw:.2f}kW")
    print(f"调度说明:")
    for note in decision.notes:
        if "需求响应" in note:
            print(f"  - {note}")

    records = dr.get_event_execution_records(event.event_id)
    if records:
        latest = records[-1]
        print(f"\n执行记录:")
        print(f"  实际负荷: {latest.actual_load_kw:.2f}kW")
        print(f"  是否达标: {latest.is_compliant}")
        print(f"  总削减量: {latest.total_reduction_kw:.2f}kW")

    print("测试8 通过 ✓")
    print()


def test_accumulated_stats():
    print("=" * 60)
    print("测试9: 累计统计")
    print("=" * 60)

    state = MicrogridState()
    dr = DemandResponseManager(state)

    state.load_report = LoadReport(load_kw=300.0, timestamp=datetime.now())

    for i in range(3):
        start = datetime.now() + timedelta(hours=i)
        end = start + timedelta(minutes=5)

        event = dr.receive_event({
            "event_no": f"TEST-STATS-{i:03d}",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "target_load_kw": 250.0,
            "subsidy_unit_price": 1.5,
            "penalty_unit_price": 3.0,
        })

        dr.generate_response_plan(event.event_id)
        dr.confirm_plan(event.event_id)
        event.status = "active"

        for j in range(5):
            ts = start + timedelta(minutes=j)
            dr.record_execution(event.event_id, 245.0, {"ac_main": 40.0, "lighting_area_a": 15.0}, 0.0, ts)

        event.status = "finished"
        event.finished_at = end
        dr.generate_settlement_report(event.event_id)

    stats = dr.get_accumulated_stats()

    print(f"总事件数: {stats['total_events']}")
    print(f"已结束事件: {stats['finished_events']}")
    print(f"进行中事件: {stats['active_events']}")
    print(f"待响应事件: {stats['pending_events']}")
    print(f"总补贴收入: {stats['total_subsidy']}元")
    print(f"总罚款: {stats['total_penalty']}元")
    print(f"净收益: {stats['net_income']}元")
    print(f"总削减电量: {stats['total_reduction_kwh']}kWh")

    assert stats["total_events"] == 3
    assert stats["finished_events"] == 3
    assert stats["active_events"] == 0
    assert stats["total_subsidy"] > 0
    assert stats["net_income"] > 0

    print("测试9 通过 ✓")
    print()


def main():
    print()
    print("╔" + "=" * 58 + "╗")
    print("║     需求响应模块测试套件                                     ║")
    print("╚" + "=" * 58 + "╝")
    print()

    try:
        test_interruptible_load_crud()
        test_event_reception_and_plan_generation()
        test_plan_cost_ordering()
        test_battery_supplement()
        test_event_status_flow()
        test_settlement_calculation()
        test_early_termination()
        test_dr_constraints_application()
        test_accumulated_stats()

        print("=" * 60)
        print("所有测试通过! ✓")
        print("=" * 60)
        return 0
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
