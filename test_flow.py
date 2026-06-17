"""
测试脚本：模拟完整的微网调度流程
包含多个场景：
1. 新能源充足 -> 电池充电 + 余电上网
2. 新能源不足 -> 电池放电 + 外购电（平时段）
3. 新能源不足，峰时段 -> 电池放电 + 柴油机
4. 所有源不足 -> 甩负荷告警
5. 柴油机最小运行时间约束
"""
import json
import time
import requests

BASE = "http://127.0.0.1:5001"


def print_sep(title=""):
    print(f"\n{'='*60}")
    if title:
        print(f"  {title}")
        print(f"{'='*60}")


def post(path, data):
    r = requests.post(f"{BASE}{path}", json=data, timeout=5)
    print(f"  POST {path} -> {r.status_code}")
    return r.json()


def get(path, params=None):
    r = requests.get(f"{BASE}{path}", params=params, timeout=5)
    print(f"  GET {path} -> {r.status_code}")
    return r.json()


def put(path, data):
    r = requests.put(f"{BASE}{path}", json=data, timeout=5)
    print(f"  PUT {path} -> {r.status_code}")
    return r.json()


def report_all(pv1=80, pv2=85, wt1=30, load=120, diesel_avail=True, auto=True):
    post("/api/source/report", {"source_id": "pv1", "source_type": "pv", "power_kw": pv1, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "pv2", "source_type": "pv", "power_kw": pv2, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "wt1", "source_type": "wt", "power_kw": wt1, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "ds1", "source_type": "diesel", "power_kw": 0, "available": diesel_avail, "auto_dispatch": False})
    return post("/api/load/report", {"load_kw": load, "auto_dispatch": auto})


print_sep("0. 健康检查 & 初始状态")
res = get("/api/health")
print(json.dumps(res, ensure_ascii=False, indent=2))

res = get("/api/config/all")
print(json.dumps(res, ensure_ascii=False, indent=2))


print_sep("场景1: 新能源充足 (PV 165kW + WT 30kW > Load 120kW)")
res = report_all(pv1=80, pv2=85, wt1=30, load=120)
print(f"  新能源出力: {80+85+30}kW, 负荷: 120kW, 盈余: {80+85+30-120}kW")
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  电池充电: {d['bess_action']['bes1']['charge_kw']:.2f}kW")
    print(f"  余电上网: {d['grid_export_kw']:.2f}kW")
    print(f"  调度成本: {d['decision_cost']:.2f}元 (负=收入)")
    print(f"  备注: {d['notes']}")
    print(f"  SOC: {d['bess_action']['bes1']['soc_before']*100:.1f}% -> {d['bess_action']['bes1']['soc_after']*100:.1f}%")


time.sleep(1.1)
print_sep("场景2: 新能源不足 (PV 50kW + WT 10kW < Load 250kW)，平时段->外购电")
res = report_all(pv1=25, pv2=25, wt1=10, load=250)
print(f"  新能源出力: {60}kW, 负荷: 250kW, 缺口: {190}kW")
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  时段: {d['tariff_period']}, 购电价: {d['grid_buy_price']}元/kWh")
    print(f"  电池放电: {d['bess_action']['bes1']['discharge_kw']:.2f}kW")
    print(f"  外购电: {d['grid_import_kw']:.2f}kW")
    print(f"  柴油机出力: {sum(d['diesel_output_kw'].values()):.2f}kW")
    print(f"  调度成本: {d['decision_cost']:.2f}元")
    print(f"  备注: {d['notes']}")


time.sleep(1.1)
print_sep("场景3: 电价 & SOC 配置修改后重新调度")
res = put("/api/config/tariff", {"peak": {"price": 1.5}, "feed_in": 0.35})
print(f"  修改电价: {json.dumps(res.get('updated', {}), ensure_ascii=False)}")

res = put("/api/config/bess_soc", {"bes_id": "bes1", "soc_min": 0.10, "soc_max": 0.95})
print(f"  修改SOC: {json.dumps(res.get('updated', {}), ensure_ascii=False)}")

res = get("/api/status/tariff")
print(f"  当前时段: {res['tariff']['period_chinese']}, 电价: {res['tariff']['grid_buy_price']}元/kWh")


time.sleep(1.1)
print_sep("场景4: 电池状态 & 发电源状态查询")
res = get("/api/status/bess")
for bid, info in res["bess"].items():
    print(f"  {bid}: SOC {info['soc_percent']}% (区间 {info['soc_min_percent']}%-{info['soc_max_percent']}%)")

res = get("/api/status/sources")
print(f"  光伏总出力: {sum(v['current_power_kw'] for v in res['sources']['photovoltaic'].values()):.1f}kW")
print(f"  风电出力: {sum(v['current_power_kw'] for v in res['sources']['wind_turbine'].values()):.1f}kW")
ds_info = list(res["sources"]["diesel_generator"].values())[0]
print(f"  柴油机: 运行={ds_info['running']}, 可启动={ds_info['can_start_now']}")


time.sleep(1.1)
print_sep("场景5: 极端大负荷 -> 甩负荷告警")
res = report_all(pv1=5, pv2=5, wt1=2, load=1500)
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  总缺口应对: 新能源{sum(d['pv_output_kw'].values())+sum(d['wt_output_kw'].values()):.0f}kW + 电池{d['bess_action']['bes1']['discharge_kw']:.0f}kW + 柴油机{sum(d['diesel_output_kw'].values()):.0f}kW + 外购{d['grid_import_kw']:.0f}kW")
    print(f"  甩负荷量: {d['load_shed_kw']:.2f}kW")
    print(f"  备注: {d['notes']}")


print_sep("场景6: 查询调度历史 (最近3条)")
res = get("/api/dispatch/history", {"limit": 3})
print(f"  总调度次数: {res['total']}")
for i, d in enumerate(res["history"]):
    src = sum(d["pv_output_kw"].values()) + sum(d["wt_output_kw"].values())
    print(f"  [{i+1}] {d['timestamp'][11:19]} 新能源{src:.0f}kW 电池{d['bess_action']['bes1']['charge_kw']+d['bess_action']['bes1']['discharge_kw']:.0f}kW 外购{d['grid_import_kw']:.0f}kW 柴油{sum(d['diesel_output_kw'].values()):.0f}kW 成本{d['decision_cost']:.1f}元")


print_sep("场景7: 累计运行统计")
res = get("/api/stats/accumulated")
s = res["stats"]
g = s["total_generation_kwh"]
print(f"  总发电量: {g['total_all_kwh']:.2f}kWh")
print(f"    - 光伏: {g['total_pv_kwh']:.2f}kWh")
print(f"    - 风电: {g['total_wind_kwh']:.2f}kWh")
print(f"    - 柴油: {g['by_source']['diesel']:.2f}kWh")
print(f"  电网交互: 购电{s['grid_interaction_kwh']['total_imported']:.2f}kWh / 售电{s['grid_interaction_kwh']['total_exported']:.2f}kWh")
print(f"  柴油机启动次数: {s['diesel']['total_starts']}次")
print(f"  甩负荷累计: {s['load_shedding']['total_shed_kwh']:.2f}kWh")
print(f"  累计成本: {s['economics']['total_cost']:.2f}元")


print_sep("场景8: 告警记录")
res = get("/api/alerts", {"limit": 10})
print(f"  总告警数: {res['total']}")
for a in res["alerts"]:
    print(f"  - [{a['type']}] {a['message']} @ {a['timestamp'][11:19]}")


print_sep("测试完成")
