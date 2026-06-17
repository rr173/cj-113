"""
测试分时段储能策略优化模块
"""
import sys
sys.path.insert(0, '/Users/chengjie/bytedance/newcj/cj-113')

from datetime import datetime
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
import config


def setup_state():
    state = MicrogridState()
    engine = DispatchEngine(state)
    return state, engine


def test_plan_generation():
    print("=" * 70)
    print("测试1: 储能计划自动生成")
    print("=" * 70)

    state, engine = setup_state()
    now = datetime(2026, 6, 17, 1, 0, 0)

    assert state.should_generate_plan(now), "过了计划生成时刻应该需要生成计划"

    plan = state.generate_storage_plan(now)
    print(f"  计划日期: {plan.plan_date}")
    print(f"  生成时间: {plan.generated_at}")
    print(f"  计划时段数: {len(plan.hours)}")

    assert len(plan.hours) == 24, "应该有24个时段"

    valley_hours = [h for h, p in plan.hours.items() if p.tariff_period == "valley"]
    peak_hours = [h for h, p in plan.hours.items() if p.tariff_period == "peak"]
    flat_hours = [h for h, p in plan.hours.items() if p.tariff_period == "flat"]

    print(f"  谷时段: {valley_hours}")
    print(f"  峰时段: {peak_hours}")
    print(f"  平时段: {flat_hours}")

    for h in valley_hours:
        assert plan.hours[h].mode in ["active_charge", "normal"], f"谷时段{h}应该是主动充电或常规模式"
    for h in peak_hours:
        assert plan.hours[h].mode == "priority_discharge", f"峰时段{h}应该是优先放电模式"
    for h in flat_hours:
        assert plan.hours[h].mode == "normal", f"平时段{h}应该是常规模式"

    assert not state.should_generate_plan(now), "同一天不应该重复生成计划"

    print("  ✅ 储能计划生成测试通过")


def test_valley_active_charge():
    print("\n" + "=" * 70)
    print("测试2: 谷时段主动充电 (即使新能源不足也从电网买电充电)")
    print("=" * 70)

    state, engine = setup_state()
    bes_id = "bes1"
    cfg = config.BESS_CONFIG[bes_id]

    state.bess_state[bes_id].soc = 0.30
    now = datetime(2026, 6, 17, 2, 0, 0)

    state.generate_storage_plan(now)

    state.pv_reports["pv1"] = SourceReport("pv1", "pv", 0.0, True, now)
    state.pv_reports["pv2"] = SourceReport("pv2", "pv", 0.0, True, now)
    state.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
    state.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
    state.load_report = LoadReport(50.0, now)

    decision = engine.execute(now)

    print(f"  初始SOC: 30%")
    print(f"  时段: 谷时段(凌晨2点)")
    print(f"  新能源出力: 0kW")
    print(f"  负荷: 50kW")
    print(f"  电池充电: {decision.bess_action[bes_id]['charge_kw']:.2f}kW")
    print(f"  电池放电: {decision.bess_action[bes_id]['discharge_kw']:.2f}kW")
    print(f"  电网购电: {decision.grid_import_kw:.2f}kW")
    print(f"  调度后SOC: {decision.bess_action[bes_id]['soc_after'] * 100:.2f}%")
    print(f"  调度Notes:")
    for note in decision.notes:
        print(f"    - {note}")

    assert decision.bess_action[bes_id]["charge_kw"] > 0, "谷时段应该主动充电"
    assert decision.grid_import_kw > 50, "应该从电网购电用于充电+供负荷"
    assert decision.bess_action[bes_id]["soc_after"] > 0.30, "SOC应该上升"

    arbitrage = state.stats.arbitrage
    print(f"\n  套利统计:")
    print(f"    套利充电量: {arbitrage.total_arbitrage_charge_kwh:.4f}kWh")
    print(f"    套利充电成本: {arbitrage.total_arbitrage_cost:.4f}元")
    assert arbitrage.total_arbitrage_charge_kwh > 0, "应该记录套利充电"
    assert arbitrage.total_arbitrage_cost > 0, "应该记录套利充电成本"

    print("  ✅ 谷时段主动充电测试通过")


def test_peak_priority_discharge():
    print("\n" + "=" * 70)
    print("测试3: 峰时段优先放电 (先放电池再买电)")
    print("=" * 70)

    state, engine = setup_state()
    bes_id = "bes1"
    cfg = config.BESS_CONFIG[bes_id]

    state.bess_state[bes_id].soc = 0.80
    now = datetime(2026, 6, 17, 13, 0, 0)

    state.generate_storage_plan(now)

    state.pv_reports["pv1"] = SourceReport("pv1", "pv", 0.0, True, now)
    state.pv_reports["pv2"] = SourceReport("pv2", "pv", 0.0, True, now)
    state.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
    state.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
    state.load_report = LoadReport(150.0, now)

    decision = engine.execute(now)

    print(f"  初始SOC: 80%")
    print(f"  时段: 峰时段(下午1点)")
    print(f"  新能源出力: 0kW")
    print(f"  负荷: 150kW")
    print(f"  电池放电: {decision.bess_action[bes_id]['discharge_kw']:.2f}kW")
    print(f"  电池最大放电功率: {cfg['max_discharge_power']}kW")
    print(f"  电网购电: {decision.grid_import_kw:.2f}kW")
    print(f"  调度后SOC: {decision.bess_action[bes_id]['soc_after'] * 100:.2f}%")
    print(f"  调度Notes:")
    for note in decision.notes:
        print(f"    - {note}")

    assert decision.bess_action[bes_id]["discharge_kw"] > 0, "峰时段应该优先放电"
    assert decision.bess_action[bes_id]["discharge_kw"] <= cfg["max_discharge_power"], "放电功率不应该超过最大功率"

    arbitrage = state.stats.arbitrage
    print(f"\n  套利统计:")
    print(f"    套利放电量: {arbitrage.total_arbitrage_discharge_kwh:.4f}kWh")
    print(f"    套利放电收益: {arbitrage.total_arbitrage_revenue:.4f}元")
    assert arbitrage.total_arbitrage_discharge_kwh > 0, "应该记录套利放电"
    assert arbitrage.total_arbitrage_revenue > 0, "应该记录套利放电收益"

    print("  ✅ 峰时段优先放电测试通过")


def test_flat_normal_mode():
    print("\n" + "=" * 70)
    print("测试4: 平时段保持常规逻辑")
    print("=" * 70)

    state, engine = setup_state()
    bes_id = "bes1"
    cfg = config.BESS_CONFIG[bes_id]

    state.bess_state[bes_id].soc = 0.50
    now = datetime(2026, 6, 17, 9, 0, 0)

    state.generate_storage_plan(now)

    state.pv_reports["pv1"] = SourceReport("pv1", "pv", 100.0, True, now)
    state.pv_reports["pv2"] = SourceReport("pv2", "pv", 100.0, True, now)
    state.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
    state.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
    state.load_report = LoadReport(50.0, now)

    decision = engine.execute(now)

    print(f"  初始SOC: 50%")
    print(f"  时段: 平时段(上午9点)")
    print(f"  新能源出力: 200kW")
    print(f"  负荷: 50kW")
    print(f"  剩余新能源: 150kW")
    print(f"  电池充电: {decision.bess_action[bes_id]['charge_kw']:.2f}kW")
    print(f"  余电上网: {decision.grid_export_kw:.2f}kW")
    print(f"  电网购电: {decision.grid_import_kw:.2f}kW")

    assert decision.grid_import_kw == 0, "平时段有新能源盈余不应主动购电充电"
    assert decision.bess_action[bes_id]["charge_kw"] > 0, "有新能源盈余应该充电"

    plan_hour = state.get_current_hour_plan(now)
    assert plan_hour.mode == "normal", "平时段应该是常规模式"

    print("  ✅ 平时段常规逻辑测试通过")


def test_soc_abnormal_suspend():
    print("\n" + "=" * 70)
    print("测试5: SOC低于下限自动暂停计划，恢复后自动继续")
    print("=" * 70)

    state, engine = setup_state()
    bes_id = "bes1"
    cfg = config.BESS_CONFIG[bes_id]

    state.generate_storage_plan(datetime(2026, 6, 17, 0, 0, 0))

    state.bess_state[bes_id].soc = cfg["soc_min"] - 0.05
    now = datetime(2026, 6, 17, 13, 0, 0)

    is_abnormal = state.check_and_handle_soc_abnormal(bes_id, now)
    print(f"  SOC设置为: {(cfg['soc_min'] - 0.05) * 100:.1f}% (下限: {cfg['soc_min'] * 100:.1f}%)")
    print(f"  检测到异常: {is_abnormal}")

    plan_hour = state.get_current_hour_plan(now)
    print(f"  当前时段计划异常标记: {plan_hour.abnormal}")
    print(f"  当前时段计划激活状态: {plan_hour.active}")

    assert plan_hour.abnormal == True, "SOC低于下限应该标记异常"
    assert plan_hour.active == False, "SOC低于下限应该暂停计划"

    alerts = [a for a in state.alerts if a["type"] == "STORAGE_PLAN_SUSPENDED"]
    print(f"  暂停告警数: {len(alerts)}")
    assert len(alerts) >= 1, "应该产生暂停告警"

    state.bess_state[bes_id].soc = cfg["soc_min"] + 0.10
    is_abnormal2 = state.check_and_handle_soc_abnormal(bes_id, now)
    print(f"\n  SOC恢复为: {(cfg['soc_min'] + 0.10) * 100:.1f}%")
    print(f"  仍检测异常: {is_abnormal2}")

    plan_hour2 = state.get_current_hour_plan(now)
    print(f"  当前时段计划异常标记: {plan_hour2.abnormal}")
    print(f"  当前时段计划激活状态: {plan_hour2.active}")

    assert plan_hour2.abnormal == False, "SOC恢复后应该清除异常标记"
    assert plan_hour2.active == True, "SOC恢复后应该激活计划"

    alerts_resume = [a for a in state.alerts if a["type"] == "STORAGE_PLAN_RESUMED"]
    print(f"  恢复告警数: {len(alerts_resume)}")
    assert len(alerts_resume) >= 1, "应该产生恢复告警"

    print("  ✅ SOC异常暂停/恢复测试通过")


def test_arbitrage_economy_check():
    print("\n" + "=" * 70)
    print("测试6: 经济性判断 - 套利不划算时谷时段不主动充电")
    print("=" * 70)

    state, engine = setup_state()

    original_valley = config.GRID_TARIFF["valley"]["price"]
    original_peak = config.GRID_TARIFF["peak"]["price"]
    original_min_ratio = config.STORAGE_STRATEGY_CONFIG["min_arbitrage_profit_ratio"]

    try:
        config.GRID_TARIFF["valley"]["price"] = 1.0
        config.GRID_TARIFF["peak"]["price"] = 1.0
        config.STORAGE_STRATEGY_CONFIG["min_arbitrage_profit_ratio"] = 0.05

        now = datetime(2026, 6, 17, 0, 0, 0)
        plan = state.generate_storage_plan(now)

        valley_modes = [plan.hours[h].mode for h in range(24) if plan.hours[h].tariff_period == "valley"]
        print(f"  谷时电价: 1.0元, 峰时电价: 1.0元 (无利可图)")
        print(f"  谷时段模式: {valley_modes}")

        assert all(m == "normal" for m in valley_modes), "无利可图时谷时段应该是常规模式而非主动充电"
        print("  ✅ 经济性判断测试通过 (无利可图时不主动充电)")

    finally:
        config.GRID_TARIFF["valley"]["price"] = original_valley
        config.GRID_TARIFF["peak"]["price"] = original_peak
        config.STORAGE_STRATEGY_CONFIG["min_arbitrage_profit_ratio"] = original_min_ratio


def test_plan_report_and_stats():
    print("\n" + "=" * 70)
    print("测试7: 储能计划报告和套利统计查询")
    print("=" * 70)

    state, engine = setup_state()
    now = datetime(2026, 6, 17, 0, 0, 0)
    state.generate_storage_plan(now)

    report = state.get_storage_plan_report()
    print(f"  计划存在: {report['plan_exists']}")
    print(f"  计划日期: {report['plan_date']}")
    print(f"  计划生成时间: {report['plan_generation_time']}")
    print(f"  时段数: {len(report['hours'])}")
    assert report["plan_exists"] == True
    assert len(report["hours"]) == 24

    arbitrage_report = state.get_arbitrage_stats_report()
    print(f"\n  初始套利统计:")
    print(f"    充电量: {arbitrage_report['total_arbitrage_charge_kwh']}")
    print(f"    放电量: {arbitrage_report['total_arbitrage_discharge_kwh']}")
    print(f"    净收益: {arbitrage_report['net_profit']}")
    assert arbitrage_report["total_arbitrage_charge_kwh"] == 0
    assert arbitrage_report["net_profit"] == 0

    success = state.update_plan_generation_time(1, 30)
    print(f"\n  修改计划生成时刻为 01:30: {success}")
    assert success == True
    assert state.plan_generation_hour == 1
    assert state.plan_generation_minute == 30

    success2 = state.update_plan_generation_time(25, 0)
    print(f"  修改计划生成时刻为 25:00 (非法): {success2}")
    assert success2 == False

    print("  ✅ 计划报告和统计查询测试通过")


def test_peak_discharge_no_derating():
    print("\n" + "=" * 70)
    print("测试8: 峰时段优先放电不降额 (即使健康度低)")
    print("=" * 70)

    state, engine = setup_state()
    bes_id = "bes1"
    cfg = config.BESS_CONFIG[bes_id]

    bh = state.bess_state[bes_id].health
    bh.equivalent_cycles = cfg["cycle_life_threshold"] * 0.85
    state._update_health_percent(bes_id)
    health_pct = bh.health_percent
    print(f"  设置健康度: {health_pct:.1f}% (低于降额阈值{cfg['health_derating_threshold']}%)")
    assert health_pct < cfg["health_derating_threshold"], "应该处于降额状态"

    state.bess_state[bes_id].soc = 0.90
    now = datetime(2026, 6, 17, 13, 0, 0)
    state.generate_storage_plan(now)

    state.pv_reports["pv1"] = SourceReport("pv1", "pv", 0.0, True, now)
    state.pv_reports["pv2"] = SourceReport("pv2", "pv", 0.0, True, now)
    state.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
    state.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
    state.load_report = LoadReport(500.0, now)

    decision = engine.execute(now)
    discharge_kw = decision.bess_action[bes_id]["discharge_kw"]
    derated_power = cfg["max_discharge_power"] * (1 - cfg["power_derating_ratio"])

    print(f"  负荷: 500kW")
    print(f"  实际放电功率: {discharge_kw:.2f}kW")
    print(f"  电池最大放电功率: {cfg['max_discharge_power']}kW")
    print(f"  降额后功率: {derated_power:.2f}kW")

    has_note = any("峰时段优先放电，放电功率不降额" in note for note in decision.notes)
    print(f"  有不降额提示: {has_note}")
    print(f"  调度Notes:")
    for note in decision.notes:
        print(f"    - {note}")

    assert discharge_kw > derated_power, "峰时段优先放电应该不降额"
    assert discharge_kw <= cfg["max_discharge_power"], "但不应该超过电池物理最大功率"

    print("  ✅ 峰时段放电不降额测试通过")


if __name__ == "__main__":
    test_plan_generation()
    test_valley_active_charge()
    test_peak_priority_discharge()
    test_flat_normal_mode()
    test_soc_abnormal_suspend()
    test_arbitrage_economy_check()
    test_plan_report_and_stats()
    test_peak_discharge_no_derating()

    print("\n" + "=" * 70)
    print("所有储能策略测试通过!")
    print("=" * 70)
