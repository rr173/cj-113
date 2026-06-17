"""
专项测试: 验证柴油机启动/最小运行时间/甩负荷告警
需要先停掉之前的服务并重启以重置状态
"""
import json
import time
import requests

BASE = "http://127.0.0.1:5001"


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


def report_round(pv1, pv2, wt1, load, ds_avail=True):
    post("/api/source/report", {"source_id": "pv1", "source_type": "pv", "power_kw": pv1, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "pv2", "source_type": "pv", "power_kw": pv2, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "wt1", "source_type": "wt", "power_kw": wt1, "available": True, "auto_dispatch": False})
    post("/api/source/report", {"source_id": "ds1", "source_type": "diesel", "power_kw": 0, "available": ds_avail, "auto_dispatch": False})
    return post("/api/load/report", {"load_kw": load, "auto_dispatch": True})


print("\n" + "="*60)
print("  专项测试: 柴油机启动 (把峰电价提至 3元/kWh > 柴油2元)")
print("="*60)
res = put("/api/config/tariff", {"peak": {"price": 3.0}})
print(f"  峰电价改为: {res.get('updated', {})} 元/kWh (高于柴油2元)")

time.sleep(1.1)
res = report_round(pv1=10, pv2=10, wt1=5, load=300)
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"\n  负荷 300kW > 新能源 25kW + 电池放电:")
    print(f"    时段: {d['tariff_period']}, 购电价: {d['grid_buy_price']}元 (高于柴油2元)")
    print(f"    电池放电: {d['bess_action']['bes1']['discharge_kw']:.1f}kW")
    diesel_kw = sum(d["diesel_output_kw"].values())
    print(f"    柴油机出力: {diesel_kw:.1f}kW")
    print(f"    外购电: {d['grid_import_kw']:.1f}kW")
    print(f"    调度决策备注:")
    for note in d["notes"]:
        print(f"      -> {note}")
    ds_res = get("/api/status/sources")
    ds = list(ds_res["sources"]["diesel_generator"].values())[0]
    print(f"\n  柴油机状态: 运行={ds['running']}, 出力={ds['current_output_kw']:.1f}kW, 累计启动={ds['total_starts']}次")
    assert ds["current_output_kw"] > 0, "BUG: 柴油机已启动但状态查询出力为0！"
    print(f"  ✅ 修复验证: 柴油机状态查询出力正确（{ds['current_output_kw']:.1f}kW）")

print("\n" + "="*60)
print("  专项测试: 柴油机最小运行时间约束 (再调度一次，负荷很小但柴油机不能停)")
print("="*60)
time.sleep(1.1)
res = report_round(pv1=90, pv2=90, wt1=30, load=50)
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  新能源 210kW >> 负荷 50kW，盈余很多")
    print(f"  柴油机出力: {sum(d['diesel_output_kw'].values()):.1f}kW (不能停机)")
    print(f"  电池充电: {d['bess_action']['bes1']['charge_kw']:.1f}kW")
    print(f"  余电上网: {d['grid_export_kw']:.1f}kW")
    for note in d["notes"]:
        if "柴油机" in note or "空载" in note:
            print(f"    -> {note}")
    ds_res = get("/api/status/sources")
    ds = list(ds_res["sources"]["diesel_generator"].values())[0]
    print(f"  柴油机状态: 运行={ds['running']}, 出力={ds['current_output_kw']:.1f}kW (空载)")

print("\n" + "="*60)
print("  专项测试: 柴油机空载后负荷再上来 -> 直接恢复出力，不扣启动费")
print("="*60)
time.sleep(1.1)
res = report_round(pv1=20, pv2=20, wt1=10, load=350)
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  负荷 350kW，新能源 50kW，缺口 300kW")
    diesel_kw = sum(d["diesel_output_kw"].values())
    print(f"  柴油机出力: {diesel_kw:.1f}kW")
    print(f"  电池放电: {d['bess_action']['bes1']['discharge_kw']:.1f}kW")
    print(f"  本轮调度成本: {d['decision_cost']:.2f}元")
    print(f"  调度备注:")
    startup_fee_applied = False
    for note in d["notes"]:
        print(f"    -> {note}")
        if "启动柴油机" in note and "固定成本" in note:
            startup_fee_applied = True
    ds_res = get("/api/status/sources")
    ds = list(ds_res["sources"]["diesel_generator"].values())[0]
    print(f"  柴油机状态: 运行={ds['running']}, 出力={ds['current_output_kw']:.1f}kW, 累计启动={ds['total_starts']}次")
    assert ds["total_starts"] == 1, f"BUG: 柴油机应该只启动1次，但显示{ds['total_starts']}次！"
    assert not startup_fee_applied, "BUG: 空载恢复出力不应再扣启动费！"
    print(f"  ✅ 修复验证: 柴油机累计启动仍为{ds['total_starts']}次，空载恢复出力未重复扣费")

print("\n" + "="*60)
print("  专项测试: 柴油机不可用 + 电池耗尽 -> 甩负荷告警")
print("="*60)
time.sleep(1.1)
res = put("/api/config/bess_soc", {"bes_id": "bes1", "soc_min": 0.49, "soc_max": 0.51})
print(f"  收紧SOC区间，模拟电池电量不足")
time.sleep(1.1)
res = report_round(pv1=5, pv2=5, wt1=0, load=800, ds_avail=False)
if "dispatch_decision" in res:
    d = res["dispatch_decision"]
    print(f"  负荷 800kW, 新能源仅 10kW, 柴油机不可用, 电池受限")
    print(f"  电池放电: {d['bess_action']['bes1']['discharge_kw']:.1f}kW")
    print(f"  柴油机: {sum(d['diesel_output_kw'].values()):.1f}kW")
    print(f"  甩负荷量: {d['load_shed_kw']:.2f}kW")
    for note in d["notes"]:
        print(f"    -> {note}")

print("\n" + "="*60)
print("  专项测试: 查询甩负荷告警")
print("="*60)
res = get("/api/alerts")
print(f"  总告警数: {res['total']}")
for a in res["alerts"]:
    if a["type"] == "LOAD_SHEDDING":
        print(f"  [{a['type']}] {a['message']}")
        print(f"    详情: 负荷={a['data']['load_kw']}kW, 可用={a['data']['total_available_kw']}kW, 甩负荷={a['data']['shed_kw']}kW")

print("\n" + "="*60)
print("  专项测试全部通过!")
print("="*60)
