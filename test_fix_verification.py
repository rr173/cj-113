"""
验证两个bug的修复:
1. 内阻检测: 不同放电功率下，劣化时应该能正确检测到
2. 降额: 健康度<30%时正确降额
"""
import sys
sys.path.insert(0, '/Users/chengjie/bytedance/newcj/cj-113')

from datetime import datetime
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
import config

print("=" * 70)
print("验证修复1: 内阻检测 - 不同放电功率下劣化应能检测")
print("=" * 70)

state = MicrogridState()
bes_id = "bes1"
bh = state.bess_state[bes_id].health
cfg = config.BESS_CONFIG[bes_id]

normal_efficiency_factor = 1.0
abnormal_efficiency_factor = 1.2  # 内阻升高后，放出同样电量SOC下降多20%

print(f"\n模拟放电:")
print(f"  前100次(基线): 不同功率，正常效率")
print(f"  后30次(最近): 不同功率，效率降低20%(SOC下降多20%)")
print(f"  阈值: {cfg['internal_resistance_degradation_ratio']*100}%")

# 模拟电池理论SOC下降: 放出energy_kwh的电，soc下降 = energy_kwh / capacity_kwh / efficiency
capacity = cfg["capacity_kwh"]
discharge_efficiency = cfg["discharge_efficiency"]

for i in range(130):
    # 不同的放电功率，验证新指标对功率不敏感
    if i % 3 == 0:
        p = 50.0
    elif i % 3 == 1:
        p = 80.0
    else:
        p = 100.0

    duration = 1.0 / 60.0  # 1分钟

    energy_out = p * duration
    soc_drop_theory = energy_out / capacity / discharge_efficiency

    if i < 100:
        # 基线: 正常效率
        soc_drop = soc_drop_theory * normal_efficiency_factor
    else:
        # 最近: 劣化，效率下降，同样放电量SOC下降更多
        soc_drop = soc_drop_theory * abnormal_efficiency_factor

    soc_before = 0.8
    soc_after = soc_before - soc_drop

    state.record_bess_discharge(bes_id, p, soc_before, soc_after, duration)
    state._update_cycle_count(bes_id, p, duration)

print(f"\n结果:")
print(f"  总记录数: {len(bh.discharge_records)}")
print(f"  基线速率: {bh.baseline_soc_drop_rate}")

if len(bh.discharge_records) >= 130:
    baseline_records = bh.discharge_records[:100]
    recent_records = bh.discharge_records[-30:]
    baseline_avg = sum(r.soc_drop_rate for r in baseline_records) / 100
    recent_avg = sum(r.soc_drop_rate for r in recent_records) / 30
    print(f"  基线平均rate: {baseline_avg}")
    print(f"  最近平均rate: {recent_avg}")
    increase = (recent_avg - baseline_avg) / baseline_avg * 100 if baseline_avg > 0 else 0
    print(f"  增长率: {increase:.2f}%")
    print(f"  阈值: {cfg['internal_resistance_degradation_ratio']*100}%")

print(f"\n  内阻异常: {bh.internal_resistance_abnormal}")
print(f"  ✅ 内阻检测: {'PASS (正确检测到劣化)' if bh.internal_resistance_abnormal else 'FAIL (未检测到劣化!)'}")

alerts = [a for a in state.alerts if a["type"] == "BATTERY_RESISTANCE_ALERT"]
print(f"  内阻告警数: {len(alerts)}")
if alerts:
    print(f"  告警信息: {alerts[0]['message']}")

print("\n" + "=" * 70)
print("验证修复2: 降额功能 - 健康度<30%时调度正确降额")
print("=" * 70)

state2 = MicrogridState()
engine2 = DispatchEngine(state2)
bh2 = state2.bess_state[bes_id].health

target_cycles = cfg["cycle_life_threshold"] * 0.80  # 健康度=20%
bh2.equivalent_cycles = target_cycles
state2._update_health_percent(bes_id)

print(f"\n调度前:")
print(f"  等效循环: {bh2.equivalent_cycles}")
print(f"  健康度: {bh2.health_percent:.2f}%")
print(f"  降额阈值: {cfg['health_derating_threshold']}%")

now = datetime.now()
state2.pv_reports["pv1"] = SourceReport("pv1", "pv", 0.0, True, now)
state2.pv_reports["pv2"] = SourceReport("pv2", "pv", 0.0, True, now)
state2.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
state2.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
state2.load_report = LoadReport(500.0, now)  # 大负荷，让电池放满功率

decision = engine2.execute(now)

print(f"\n调度结果:")
print(f"  电池放电: {decision.bess_action[bes_id]['discharge_kw']:.1f}kW")
print(f"  电池配置最大功率: {cfg['max_discharge_power']}kW")
expected_power = cfg["max_discharge_power"] * (1 - cfg["power_derating_ratio"])
print(f"  期望降额后功率: {expected_power:.1f}kW")

derating_ok = abs(decision.bess_action[bes_id]["discharge_kw"] - expected_power) < 0.1
print(f"  ✅ 放电功率降额: {'PASS' if derating_ok else 'FAIL'}")

note_ok = any("降额" in note for note in decision.notes)
print(f"  ✅ 调度notes降额提示: {'PASS' if note_ok else 'FAIL (缺少降额提示!)'}")
print(f"\n调度Notes:")
for note in decision.notes:
    print(f"    -> {note}")

print("\n" + "=" * 70)
print("验证修复3: 模拟逐步放电，健康报告API正确返回状态")
print("=" * 70)

report = state2.get_battery_health_report(bes_id)
print(f"\n健康报告:")
print(f"  健康度: {report['health_percent']}%")
print(f"  功率降额激活: {report['power_derating_active']}")
print(f"  内阻异常: {report['internal_resistance_abnormal']}")
print(f"  ✅ 健康报告功率降额标记: {'PASS' if report['power_derating_active'] else 'FAIL'}")

print("\n" + "=" * 70)
print("所有验证完成!")
print("=" * 70)
