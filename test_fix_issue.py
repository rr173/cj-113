from datetime import datetime, timedelta
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from models import MicrogridState, LoadReport, SourceReport
from demand_response import DemandResponseManager
from dispatcher import DispatchEngine


def test_reproduce_user_issue():
    print("=" * 60)
    print("复现用户问题: 调度后负荷350kW, 未削减到200kW目标")
    print("=" * 60)

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

    now = datetime.now()
    start_time = now - timedelta(minutes=5)
    end_time = now + timedelta(hours=1)

    print(f"创建事件: start={start_time}, end={end_time}, target=200kW")
    event = dr.receive_event({
        "event_no": "USER-ISSUE-001",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "target_load_kw": 200.0,
        "subsidy_unit_price": 1.5,
        "penalty_unit_price": 3.0,
    })
    print(f"  事件ID: {event.event_id}")
    print(f"  初始状态: {event.status}")

    print("\n生成响应方案...")
    plan = dr.generate_response_plan(event.event_id)
    print(f"  方案ID: {plan.plan_id}")
    print(f"  需削减: {plan.total_reduction_target_kw:.2f}kW")
    print(f"  方案说明:")
    for note in plan.notes:
        print(f"    - {note}")
    if plan.schedule:
        first_period = plan.schedule[0]
        print(f"  第一个时段: {first_period.period_start} ~ {first_period.period_end}")
        print(f"    削减明细: {first_period.load_reductions}")
        print(f"    电池放电: {first_period.battery_discharge_kw}kW")

    print("\n确认方案...")
    success = dr.confirm_plan(event.event_id)
    print(f"  确认结果: {success}")
    print(f"  确认后状态: {event.status}")

    print("\n检查当前需求响应状态...")
    dr_status = dr.get_current_reduction(now)
    print(f"  active: {dr_status['active']}")
    print(f"  target_load_kw: {dr_status['target_load_kw']}")
    print(f"  load_reductions: {dr_status.get('load_reductions', {})}")
    print(f"  battery_discharge_kw: {dr_status.get('battery_discharge_kw', 0)}")
    print(f"  debug_reason: {dr_status.get('debug_reason')}")

    active_event = dr.get_active_event(now)
    print(f"  active_event: {active_event.event_id if active_event else None}")

    all_events = [(e.event_id, e.status, e.start_time <= now <= e.end_time) for e in dr.events.values()]
    print(f"  所有事件: {all_events}")

    print("\n执行调度...")
    decision = engine.execute()

    print(f"  调度后负荷: {decision.load_served_kw:.2f}kW")
    print(f"  甩负荷量: {decision.load_shed_kw:.2f}kW")
    print(f"  调度Notes (需求响应相关):")
    dr_notes = [n for n in decision.notes if "需求响应" in n]
    if dr_notes:
        for n in dr_notes:
            print(f"    - {n}")
    else:
        print("    (无需求响应相关的notes!)")

    records = dr.get_event_execution_records(event.event_id)
    print(f"\n执行记录数量: {len(records)}")
    if records:
        latest = records[-1]
        print(f"  最新记录:")
        print(f"    实际负荷: {latest.actual_load_kw:.2f}kW")
        print(f"    目标负荷: {latest.target_load_kw:.2f}kW")
        print(f"    是否达标: {latest.is_compliant}")
        print(f"    总削减: {latest.total_reduction_kw:.2f}kW")
    else:
        print("  (空!!)")

    print()
    if decision.load_served_kw <= 200.0:
        print("✓ 问题已修复: 负荷成功削减到目标以下")
    else:
        print("✗ 问题仍然存在: 负荷未达标")
    if dr_notes:
        print("✓ 调度notes中有需求响应信息")
    else:
        print("✗ 调度notes中没有需求响应信息")
    if records:
        print("✓ 执行记录已生成")
    else:
        print("✗ 执行记录为空")

    return decision.load_served_kw <= 200.0 and bool(dr_notes) and len(records) > 0


def main():
    print()
    success = test_reproduce_user_issue()
    print("=" * 60)
    if success:
        print("所有验证通过 ✓")
        return 0
    else:
        print("验证失败 ✗")
        return 1


if __name__ == "__main__":
    sys.exit(main())
