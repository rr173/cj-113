"""
模拟完整调度流程，复现降额不生效问题
"""
import sys
sys.path.insert(0, '/Users/chengjie/bytedance/newcj/cj-113')

from datetime import datetime
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
import config

state = MicrogridState()
engine = DispatchEngine(state)

bes_id = "bes1"
bs = state.bess_state[bes_id]
bh = bs.health
cfg = config.BESS_CONFIG[bes_id]

print("=" * 70)
print("模拟: 手动将循环次数刷到很高，让健康度<30%，然后执行调度")
print("=" * 70)

# 先设置健康度很低
target_cycles = cfg["cycle_life_threshold"] * 0.80  # 健康度 = (1-0.8)*100 = 20%
bh.equivalent_cycles = target_cycles
state._update_health_percent(bes_id)

print(f"\n调度前状态:")
print(f"  等效循环次数: {bh.equivalent_cycles}")
print(f"  健康度: {bh.health_percent}%")
print(f"  降额阈值: {cfg['health_derating_threshold']}%")
print(f"  是否应该降额: {bh.health_percent < cfg['health_derating_threshold']}")
print(f"  SOC: {bs.soc*100:.1f}%")

# 手动上报数据并触发调度
now = datetime.now()
state.pv_reports["pv1"] = SourceReport("pv1", "pv", 0.0, True, now)
state.pv_reports["pv2"] = SourceReport("pv2", "pv", 0.0, True, now)
state.wt_reports["wt1"] = SourceReport("wt1", "wt", 0.0, True, now)
state.diesel_reports["ds1"] = SourceReport("ds1", "diesel", 0.0, False, now)
state.load_report = LoadReport(200.0, now)

print(f"\n上报数据: 负荷=200kW, 新能源=0kW, 柴油机不可用")
print("执行调度...")

decision = engine.execute(now)

print(f"\n调度结果:")
print(f"  电池放电: {decision.bess_action[bes_id]['discharge_kw']:.1f}kW")
print(f"  电池充电: {decision.bess_action[bes_id]['charge_kw']:.1f}kW")
print(f"  外购电: {decision.grid_import_kw:.1f}kW")
print(f"  甩负荷: {decision.load_shed_kw:.1f}kW")
print(f"\n调度Notes:")
for note in decision.notes:
    print(f"  -> {note}")

# 检查电池配置的最大放电功率
print(f"\n电池配置:")
print(f"  配置最大放电功率: {cfg['max_discharge_power']}kW")
print(f"  健康度调整后最大放电功率(方法调用): {state.get_bess_max_discharge_with_health(bes_id, 1.0/60.0):.1f}kW")

print(f"\n调度后健康度: {bh.health_percent}%")
print(f"是否降额生效: {decision.bess_action[bes_id]['discharge_kw'] < cfg['max_discharge_power'] * 0.9}")
