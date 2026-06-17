"""
电池健康模块单元测试
"""
import sys
sys.path.insert(0, '/Users/chengjie/bytedance/newcj/cj-113')

from models import MicrogridState
import config

print("=" * 60)
print("电池健康模块单元测试")
print("=" * 60)

state = MicrogridState()
bes_id = "bes1"
bs = state.bess_state[bes_id]
bh = bs.health

print("\n1. 测试初始状态")
print(f"   初始等效循环次数: {bh.equivalent_cycles}")
print(f"   初始健康度: {bh.health_percent}%")
print(f"   初始放电记录数: {len(bh.discharge_records)}")
assert bh.equivalent_cycles == 0.0
assert bh.health_percent == 100.0
assert len(bh.discharge_records) == 0
print("   ✅ 初始状态正确")

print("\n2. 测试记录放电和循环次数")
discharge_kw = 100.0
duration_hours = 1.0
soc_before = 0.8
soc_after = 0.6

state.record_bess_discharge(bes_id, discharge_kw, soc_before, soc_after, duration_hours)
state._update_cycle_count(bes_id, discharge_kw, duration_hours)

print(f"   放电功率: {discharge_kw}kW, 时长: {duration_hours}h")
print(f"   SOC: {soc_before*100}% -> {soc_after*100}%")
print(f"   放电量: {discharge_kw * duration_hours}kWh")
print(f"   电池容量: {config.BESS_CONFIG[bes_id]['capacity_kwh']}kWh")
print(f"   等效循环次数: {bh.equivalent_cycles}")
print(f"   放电记录数: {len(bh.discharge_records)}")

expected_cycles = (discharge_kw * duration_hours) / config.BESS_CONFIG[bes_id]["capacity_kwh"]
print(f"   预期循环次数: {expected_cycles}")

assert len(bh.discharge_records) == 1
assert abs(bh.equivalent_cycles - expected_cycles) < 0.001
print("   ✅ 放电记录和循环次数计算正确")

print("\n3. 测试健康度计算")
print(f"   健康度: {bh.health_percent}%")
expected_health = 100 * (1 - expected_cycles / config.BESS_CONFIG[bes_id]["cycle_life_threshold"])
print(f"   预期健康度: {expected_health}%")
assert abs(bh.health_percent - expected_health) < 0.01
print("   ✅ 健康度计算正确")

print("\n4. 测试内阻监测（需要积累足够记录）")
for i in range(150):
    p = 80.0 + i * 0.1
    s_before = 0.9 - i * 0.005
    s_after = s_before - 0.05
    state.record_bess_discharge(bes_id, p, s_before, s_after, 0.5)

print(f"   总放电记录数: {len(bh.discharge_records)}")
print(f"   基线SOC下降速率: {bh.baseline_soc_drop_rate}")
print(f"   内阻异常: {bh.internal_resistance_abnormal}")

assert len(bh.discharge_records) > 100
assert bh.baseline_soc_drop_rate is not None
print("   ✅ 内阻基线计算正确")

print("\n5. 测试重置基线")
success = state.reset_baseline(bes_id)
print(f"   重置成功: {success}")
print(f"   新基线: {bh.baseline_soc_drop_rate}")
assert success == True
print("   ✅ 基线重置功能正常")

print("\n6. 测试健康度报告")
report = state.get_battery_health_report(bes_id)
print(f"   电池ID: {report['bes_id']}")
print(f"   等效循环次数: {report['equivalent_cycles']}")
print(f"   健康度: {report['health_percent']}%")
print(f"   内阻异常: {report['internal_resistance_abnormal']}")
print(f"   预计剩余循环: {report['estimated_remaining_cycles']}")
print(f"   功率降额激活: {report['power_derating_active']}")
print(f"   最近趋势记录数: {len(report['recent_soc_drop_trend'])}")
assert report['bes_id'] == bes_id
assert report['health_percent'] > 0
print("   ✅ 健康度报告生成正确")

print("\n7. 测试降额功能")
cfg = config.BESS_CONFIG[bes_id]
print(f"   健康度降额阈值: {cfg['health_derating_threshold']}%")

max_discharge_base = state.get_bess_max_discharge(bes_id, 1.0)
max_discharge_health = state.get_bess_max_discharge_with_health(bes_id, 1.0)
print(f"   基础最大放电功率: {max_discharge_base}kW")
print(f"   健康度调整后: {max_discharge_health}kW")
assert max_discharge_base == max_discharge_health  # 健康度还高，应该不降额
print("   ✅ 健康度高时不降额")

print("\n   模拟低健康度场景...")
bh.equivalent_cycles = cfg["cycle_life_threshold"] * 0.8
bh.health_percent = 20.0  # 手动设为低于30%

max_discharge_base2 = state.get_bess_max_discharge(bes_id, 1.0)
max_discharge_health2 = state.get_bess_max_discharge_with_health(bes_id, 1.0)
print(f"   基础最大放电功率: {max_discharge_base2}kW")
print(f"   健康度调整后: {max_discharge_health2}kW")
print(f"   降额比例: {(1 - max_discharge_health2/max_discharge_base2)*100:.1f}%")

expected_derating = cfg["power_derating_ratio"]
actual_derating = 1 - max_discharge_health2 / max_discharge_base2
assert abs(actual_derating - expected_derating) < 0.01
print("   ✅ 健康度低时正确降额")

print("\n" + "=" * 60)
print("所有单元测试通过！")
print("=" * 60)
