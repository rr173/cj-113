"""
调试两个问题：
1. 内阻检测不触发
2. 降额不生效
"""
import sys
sys.path.insert(0, '/Users/chengjie/bytedance/newcj/cj-113')

from models import MicrogridState, DischargeRecord
from datetime import datetime
import config

print("=" * 70)
print("问题1调试: 内阻检测为什么不触发?")
print("=" * 70)

state = MicrogridState()
bes_id = "bes1"
bs = state.bess_state[bes_id]
bh = bs.health

cfg = config.BESS_CONFIG[bes_id]
print(f"\n配置:")
print(f"  基线记录数: {cfg['baseline_discharge_count']}")
print(f"  最近记录数: {cfg['recent_discharge_count']}")
print(f"  劣化阈值: {cfg['internal_resistance_degradation_ratio']*100}%")

print("\n模拟放电，前100次基线是正常速率，后30次是2倍速率（应该触发异常）...")

for i in range(130):
    if i < 100:
        # 基线：正常速率，每次放掉0.005 SOC
        soc_before = 0.9 - i * 0.005
        soc_after = soc_before - 0.005
        p = 80.0
    else:
        # 最近：劣化速率，每次放掉0.01 SOC（2倍！）
        soc_before = 0.9 - 100 * 0.005 - (i - 100) * 0.01
        soc_after = soc_before - 0.01
        p = 80.0

    state.record_bess_discharge(bes_id, p, soc_before, soc_after, 0.5)
    state._update_cycle_count(bes_id, p, 0.5)

print(f"\n结果:")
print(f"  总记录数: {len(bh.discharge_records)}")
print(f"  基线速率: {bh.baseline_soc_drop_rate}")

rec100 = bh.discharge_records[99]
rec129 = bh.discharge_records[129]
print(f"  第100条记录(基线末尾): rate={rec100.soc_drop_rate}, soc_drop={rec100.soc_before - rec100.soc_after}, p={rec100.discharge_power_kw}")
print(f"  第130条记录(最近末尾): rate={rec129.soc_drop_rate}, soc_drop={rec129.soc_before - rec129.soc_after}, p={rec129.discharge_power_kw}")

if len(bh.discharge_records) >= 130:
    baseline_records = bh.discharge_records[:100]
    recent_records = bh.discharge_records[-30:]
    baseline_avg = sum(r.soc_drop_rate for r in baseline_records) / 100
    recent_avg = sum(r.soc_drop_rate for r in recent_records) / 30
    print(f"\n手动计算:")
    print(f"  基线平均rate: {baseline_avg}")
    print(f"  最近平均rate: {recent_avg}")
    if baseline_avg > 0:
        print(f"  增长率: {(recent_avg - baseline_avg)/baseline_avg*100:.2f}%")
        print(f"  阈值: {cfg['internal_resistance_degradation_ratio']*100}%")

print(f"\n  内阻异常: {bh.internal_resistance_abnormal}")
print(f"  ❌ 期望: True, 实际: {bh.internal_resistance_abnormal}")

print("\n" + "=" * 70)
print("问题2调试: 健康度<30%为什么不降额?")
print("=" * 70)

state2 = MicrogridState()
bs2 = state2.bess_state[bes_id]
bh2 = bs2.health

# 手动设置循环次数让健康度降到25%
target_cycles = cfg["cycle_life_threshold"] * 0.75  # 健康度 = (1-0.75)*100 = 25%
bh2.equivalent_cycles = target_cycles
state2._update_health_percent(bes_id)

print(f"\n设置:")
print(f"  等效循环次数: {bh2.equivalent_cycles}")
print(f"  寿命阈值: {cfg['cycle_life_threshold']}")
print(f"  健康度: {bh2.health_percent}%")
print(f"  降额阈值: {cfg['health_derating_threshold']}%")

base_discharge = state2.get_bess_max_discharge(bes_id, 1.0)
health_discharge = state2.get_bess_max_discharge_with_health(bes_id, 1.0)
base_charge = state2.get_bess_max_charge(bes_id, 1.0)
health_charge = state2.get_bess_max_charge_with_health(bes_id, 1.0)

print(f"\n最大放电功率:")
print(f"  基础: {base_discharge}kW")
print(f"  健康调整: {health_discharge}kW")
print(f"  降额比例: {(1 - health_discharge/base_discharge)*100:.1f}%")
print(f"  ❌ 期望降额20%, 实际: {health_discharge == base_discharge and '未降额!' or '已降额'}")

print(f"\n最大充电功率:")
print(f"  基础: {base_charge}kW")
print(f"  健康调整: {health_charge}kW")
print(f"  降额比例: {(1 - health_charge/base_charge)*100:.1f}%")
