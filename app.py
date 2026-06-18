from datetime import datetime
from flask import Flask, request, jsonify
from dataclasses import asdict

import config
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
from demand_response import DemandResponseManager

app = Flask(__name__)

state = MicrogridState()
dr_manager = DemandResponseManager(state)
engine = DispatchEngine(state, dr_manager)


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if hasattr(obj, "__dict__"):
        return _serialize(obj.__dict__)
    return obj


def _decision_to_dict(d):
    return {
        "timestamp": d.timestamp.isoformat(),
        "pv_output_kw": d.pv_output,
        "wt_output_kw": d.wt_output,
        "diesel_output_kw": d.diesel_output,
        "bess_action": {
            bid: {
                "charge_kw": ba["charge_kw"],
                "discharge_kw": ba["discharge_kw"],
                "soc_before": ba["soc_before"],
                "soc_after": ba["soc_after"],
            }
            for bid, ba in d.bess_action.items()
        },
        "grid_import_kw": d.grid_import_kw,
        "grid_export_kw": d.grid_export_kw,
        "load_served_kw": d.load_served_kw,
        "load_shed_kw": d.load_shed_kw,
        "decision_cost": d.cost,
        "tariff_period": d.tariff_period,
        "grid_buy_price": d.grid_buy_price,
        "notes": d.notes,
    }


@app.route("/api/source/report", methods=["POST"])
def report_source():
    """
    发电源实时出力上报
    请求体: {
        "source_id": "pv1",
        "source_type": "pv" | "wt" | "diesel",
        "power_kw": 85.5,
        "available": true
    }
    """
    data = request.get_json(force=True)
    required = ["source_id", "source_type", "power_kw", "available"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    valid_types = {"pv", "wt", "diesel"}
    if data["source_type"] not in valid_types:
        return jsonify({"error": f"source_type 必须是 {valid_types} 之一"}), 400

    report = SourceReport(
        source_id=data["source_id"],
        source_type=data["source_type"],
        power_kw=float(data["power_kw"]),
        available=bool(data["available"]),
        timestamp=datetime.now(),
    )
    state.report_source(report)

    try_dispatch = data.get("auto_dispatch", True)
    if try_dispatch and state.all_sources_reported():
        decision = engine.execute()
        return jsonify({
            "status": "ok",
            "message": "数据已接收，已执行调度决策",
            "report": {
                "source_id": report.source_id,
                "source_type": report.source_type,
                "power_kw": report.power_kw,
                "available": report.available,
                "timestamp": report.timestamp.isoformat(),
            },
            "dispatch_decision": _decision_to_dict(decision),
        })

    return jsonify({
        "status": "ok",
        "message": "数据已接收，等待其它源上报后执行调度",
        "report": {
            "source_id": report.source_id,
            "source_type": report.source_type,
            "power_kw": report.power_kw,
            "available": report.available,
            "timestamp": report.timestamp.isoformat(),
        },
        "awaiting": _get_awaiting(),
    })


@app.route("/api/load/report", methods=["POST"])
def report_load():
    """
    负荷端上报当前总消耗
    请求体: {"load_kw": 350.2}
    """
    data = request.get_json(force=True)
    if "load_kw" not in data:
        return jsonify({"error": "缺少必填字段: load_kw"}), 400

    report = LoadReport(
        load_kw=float(data["load_kw"]),
        timestamp=datetime.now(),
    )
    state.report_load(report)

    try_dispatch = data.get("auto_dispatch", True)
    if try_dispatch and state.all_sources_reported():
        decision = engine.execute()
        return jsonify({
            "status": "ok",
            "message": "负荷数据已接收，已执行调度决策",
            "report": {
                "load_kw": report.load_kw,
                "timestamp": report.timestamp.isoformat(),
            },
            "dispatch_decision": _decision_to_dict(decision),
        })

    return jsonify({
        "status": "ok",
        "message": "负荷数据已接收，等待其它源上报后执行调度",
        "report": {
            "load_kw": report.load_kw,
            "timestamp": report.timestamp.isoformat(),
        },
        "awaiting": _get_awaiting(),
    })


@app.route("/api/dispatch/trigger", methods=["POST"])
def trigger_dispatch():
    """
    手动触发调度决策（即使所有数据未齐也尝试执行）
    """
    try:
        decision = engine.execute()
        return jsonify({
            "status": "ok",
            "dispatch_decision": _decision_to_dict(decision),
        })
    except ValueError as e:
        return jsonify({"error": str(e), "awaiting": _get_awaiting()}), 400


def _get_awaiting():
    result = {}
    for sid in config.PV_CONFIG:
        result[f"pv:{sid}"] = sid in state.pv_reports
    for sid in config.WT_CONFIG:
        result[f"wt:{sid}"] = sid in state.wt_reports
    result["load"] = state.load_report is not None
    return result


@app.route("/api/status/sources", methods=["GET"])
def get_sources_status():
    """
    查询各发电源实时状态和出力
    """
    now = datetime.now()
    result = {"photovoltaic": {}, "wind_turbine": {}, "diesel_generator": {}}

    for sid, cfg in config.PV_CONFIG.items():
        r = state.pv_reports.get(sid)
        result["photovoltaic"][sid] = {
            "name": cfg["name"],
            "rated_power_kw": cfg["rated_power"],
            "current_power_kw": r.power_kw if r else 0.0,
            "available": r.available if r else False,
            "last_report": r.timestamp.isoformat() if r else None,
        }

    for sid, cfg in config.WT_CONFIG.items():
        r = state.wt_reports.get(sid)
        result["wind_turbine"][sid] = {
            "name": cfg["name"],
            "rated_power_kw": cfg["rated_power"],
            "current_power_kw": r.power_kw if r else 0.0,
            "available": r.available if r else False,
            "last_report": r.timestamp.isoformat() if r else None,
        }

    for sid, cfg in config.DIESEL_CONFIG.items():
        r = state.diesel_reports.get(sid)
        ds = state.diesel_state[sid]
        cap = state.get_available_diesel_capacity(sid, now)
        result["diesel_generator"][sid] = {
            "name": cfg["name"],
            "rated_power_kw": cfg["rated_power"],
            "current_output_kw": ds.output_kw,
            "running": ds.running,
            "available": r.available if r else False,
            "last_start": ds.last_start_time.isoformat() if ds.last_start_time else None,
            "last_stop": ds.last_stop_time.isoformat() if ds.last_stop_time else None,
            "total_starts": ds.total_starts,
            "total_generated_kwh": ds.total_generated_kwh,
            "min_runtime_minutes": cfg["min_runtime_minutes"],
            "cooldown_minutes": cfg["cooldown_minutes"],
            "can_start_now": cap.get("can_run", False),
            "last_report": r.timestamp.isoformat() if r else None,
        }

    return jsonify({"status": "ok", "sources": result, "query_time": now.isoformat()})


@app.route("/api/status/bess", methods=["GET"])
def get_bess_status():
    """
    查询电池当前SOC和充放电状态
    """
    result = {}
    now = datetime.now()
    for bid, cfg in config.BESS_CONFIG.items():
        bs = state.bess_state[bid]
        result[bid] = {
            "name": cfg["name"],
            "capacity_kwh": cfg["capacity_kwh"],
            "max_charge_power_kw": cfg["max_charge_power"],
            "max_discharge_power_kw": cfg["max_discharge_power"],
            "soc_percent": round(bs.soc * 100, 2),
            "soc_min_percent": round(cfg["soc_min"] * 100, 2),
            "soc_max_percent": round(cfg["soc_max"] * 100, 2),
            "current_charge_kw": bs.charge_power_kw,
            "current_discharge_kw": bs.discharge_power_kw,
            "charge_efficiency": cfg["charge_efficiency"],
            "discharge_efficiency": cfg["discharge_efficiency"],
            "total_charged_kwh": bs.total_charged_kwh,
            "total_discharged_kwh": bs.total_discharged_kwh,
        }
    return jsonify({"status": "ok", "bess": result, "query_time": now.isoformat()})


@app.route("/api/battery/health", methods=["GET"])
def get_battery_health():
    """
    查询电池健康报告
    参数: bes_id (可选，默认bes1)
    """
    bes_id = request.args.get("bes_id", "bes1")
    if bes_id not in config.BESS_CONFIG:
        return jsonify({"error": f"未找到储能设备: {bes_id}"}), 404

    report = state.get_battery_health_report(bes_id)
    return jsonify({
        "status": "ok",
        "health_report": report,
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/battery/health/reset-baseline", methods=["POST"])
def reset_battery_baseline():
    """
    手动重置电池内阻基线
    请求体: {"bes_id": "bes1"}
    """
    data = request.get_json(force=True) or {}
    bes_id = data.get("bes_id", "bes1")

    if bes_id not in config.BESS_CONFIG:
        return jsonify({"error": f"未找到储能设备: {bes_id}"}), 404

    success = state.reset_baseline(bes_id)
    if not success:
        cfg = config.BESS_CONFIG[bes_id]
        return jsonify({
            "status": "error",
            "message": f"放电记录不足，需要至少{cfg['baseline_discharge_count']}次放电数据才能重置基线",
            "current_records": len(state.bess_state[bes_id].health.discharge_records),
            "required": cfg["baseline_discharge_count"],
        }), 400

    report = state.get_battery_health_report(bes_id)
    return jsonify({
        "status": "ok",
        "message": "基线已重置",
        "health_report": report,
    })


@app.route("/api/status/tariff", methods=["GET"])
def get_tariff_status():
    """
    查询当前时段电价
    """
    now = datetime.now()
    period = config.get_tariff_period(now.hour)
    result = {
        "current_time": now.isoformat(),
        "current_hour": now.hour,
        "tariff_period": period,
        "period_chinese": {"valley": "谷时段", "flat": "平时段", "peak": "峰时段"}.get(period, "未知"),
        "grid_buy_price": config.GRID_TARIFF[period]["price"],
        "feed_in_price": config.FEED_IN_TARIFF,
        "diesel_generation_cost": config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"],
        "diesel_startup_cost": config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["startup_cost"],
        "all_tariffs": {
            "valley": {"price": config.GRID_TARIFF["valley"]["price"], "hours": "23:00 - 07:00"},
            "flat": {"price": config.GRID_TARIFF["flat"]["price"], "hours": "07:00 - 11:00, 15:00 - 23:00"},
            "peak": {"price": config.GRID_TARIFF["peak"]["price"], "hours": "11:00 - 15:00"},
        }
    }
    return jsonify({"status": "ok", "tariff": result})


@app.route("/api/status/load", methods=["GET"])
def get_load_status():
    """查询当前负荷状态"""
    now = datetime.now()
    load = state.load_report
    return jsonify({
        "status": "ok",
        "load": {
            "current_load_kw": load.load_kw if load else 0.0,
            "last_report": load.timestamp.isoformat() if load else None,
        },
        "total_renewable_kw": state.get_total_renewable_kw(),
        "query_time": now.isoformat(),
    })


@app.route("/api/dispatch/history", methods=["GET"])
def get_dispatch_history():
    """
    查询调度决策历史
    参数: limit (可选，默认50), offset (可选，默认0)
    """
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit 和 offset 必须是整数"}), 400

    history = state.dispatch_history
    total = len(history)
    sliced = history[max(0, total - limit - offset): max(0, total - offset)] if offset > 0 else history[-limit:]
    sliced = list(reversed(sliced))

    return jsonify({
        "status": "ok",
        "total": total,
        "returned": len(sliced),
        "limit": limit,
        "offset": offset,
        "history": [_decision_to_dict(d) for d in sliced],
    })


@app.route("/api/stats/accumulated", methods=["GET"])
def get_accumulated_stats():
    """
    查询累计运行统计
    """
    s = state.stats
    now = datetime.now()
    pv_by_source = {sid: kwh for sid, kwh in s.total_pv_generated_kwh.items()}
    wt_by_source = {sid: kwh for sid, kwh in s.total_wt_generated_kwh.items()}
    total_pv = sum(pv_by_source.values())
    total_wt = sum(wt_by_source.values())
    total_renewable = total_pv + total_wt
    total_generation = total_renewable + s.total_diesel_generated_kwh

    arbitrage_net = s.arbitrage.total_arbitrage_revenue - s.arbitrage.total_arbitrage_cost

    return jsonify({
        "status": "ok",
        "stats": {
            "total_generation_kwh": {
                "by_source": {
                    "photovoltaic": pv_by_source,
                    "wind_turbine": wt_by_source,
                    "diesel": s.total_diesel_generated_kwh,
                },
                "total_pv_kwh": total_pv,
                "total_wind_kwh": total_wt,
                "total_renewable_kwh": total_renewable,
                "total_all_kwh": total_generation,
            },
            "grid_interaction_kwh": {
                "total_imported": s.total_grid_import_kwh,
                "total_load_imported": s.load_grid_import_kwh,
                "total_exported": s.total_grid_export_kwh,
                "net_imported": s.total_grid_import_kwh - s.total_grid_export_kwh,
            },
            "diesel": {
                "total_starts": s.total_diesel_starts,
                "total_generated_kwh": s.total_diesel_generated_kwh,
            },
            "load_shedding": {
                "total_shed_kwh": s.total_load_shed_kwh,
            },
            "economics": {
                "total_cost": round(s.total_cost, 2),
                "feed_in_tariff": config.FEED_IN_TARIFF,
            },
            "storage_arbitrage": {
                "total_arbitrage_charge_kwh": round(s.arbitrage.total_arbitrage_charge_kwh, 4),
                "total_arbitrage_discharge_kwh": round(s.arbitrage.total_arbitrage_discharge_kwh, 4),
                "arbitrage_cost": round(s.arbitrage.total_arbitrage_cost, 4),
                "arbitrage_revenue": round(s.arbitrage.total_arbitrage_revenue, 4),
                "arbitrage_net_profit": round(arbitrage_net, 4),
            },
        },
        "dispatch_count": len(state.dispatch_history),
        "query_time": now.isoformat(),
    })


@app.route("/api/storage/plan", methods=["GET"])
def get_storage_plan():
    """
    查询当前生效的储能计划
    """
    report = state.get_storage_plan_report()
    return jsonify({
        "status": "ok",
        "plan": report,
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/storage/plan/regenerate", methods=["POST"])
def regenerate_storage_plan():
    """
    手动触发重新生成储能计划
    """
    now = datetime.now()
    plan = state.generate_storage_plan(now)
    report = state.get_storage_plan_report()
    return jsonify({
        "status": "ok",
        "message": f"储能计划已重新生成，计划日期: {plan.plan_date}",
        "plan": report,
        "generated_at": now.isoformat(),
    })


@app.route("/api/storage/plan/generation-time", methods=["PUT"])
def update_plan_generation_time():
    """
    修改储能计划生成时刻
    请求体: {"hour": 0, "minute": 30}
    """
    data = request.get_json(force=True) or {}
    hour = data.get("hour")
    minute = data.get("minute")

    if hour is None or minute is None:
        return jsonify({"error": "缺少必填字段: hour, minute"}), 400

    try:
        hour = int(hour)
        minute = int(minute)
    except (ValueError, TypeError):
        return jsonify({"error": "hour 和 minute 必须是整数"}), 400

    success = state.update_plan_generation_time(hour, minute)
    if not success:
        return jsonify({"error": "hour 必须在 [0,23]，minute 必须在 [0,59]"}), 400

    return jsonify({
        "status": "ok",
        "message": f"计划生成时刻已更新为 {hour:02d}:{minute:02d}",
        "plan_generation_time": f"{hour:02d}:{minute:02d}",
    })


@app.route("/api/storage/arbitrage/stats", methods=["GET"])
def get_arbitrage_stats():
    """
    查询储能套利统计
    """
    report = state.get_arbitrage_stats_report()
    return jsonify({
        "status": "ok",
        "arbitrage_stats": report,
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    """查询告警记录"""
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    return jsonify({
        "status": "ok",
        "total": len(state.alerts),
        "alerts": state.alerts[-limit:],
    })


@app.route("/api/config/tariff", methods=["PUT"])
def update_tariff_config():
    """
    修改电价配置（立刻生效）
    请求体: {
        "valley": {"price": 0.35},
        "flat": {"price": 0.75},
        "peak": {"price": 1.15},
        "feed_in": 0.35
    }
    """
    data = request.get_json(force=True)
    updated = {}

    for period in ["valley", "flat", "peak"]:
        if period in data and "price" in data[period]:
            new_price = float(data[period]["price"])
            if new_price < 0:
                return jsonify({"error": f"{period} 电价不能为负"}), 400
            config.GRID_TARIFF[period]["price"] = new_price
            updated[f"{period}_price"] = new_price

    if "feed_in" in data:
        new_feed = float(data["feed_in"])
        if new_feed < 0:
            return jsonify({"error": "上网电价不能为负"}), 400
        config.FEED_IN_TARIFF = new_feed
        updated["feed_in_price"] = new_feed

    if not updated:
        return jsonify({"error": "未提供有效的电价配置修改"}), 400

    return jsonify({
        "status": "ok",
        "message": "电价配置已更新，立即生效",
        "updated": updated,
        "current_tariff": {
            p: config.GRID_TARIFF[p]["price"] for p in ["valley", "flat", "peak"]
        },
        "feed_in_tariff": config.FEED_IN_TARIFF,
    })


@app.route("/api/config/bess_soc", methods=["PUT"])
def update_bess_soc_config():
    """
    修改电池SOC工作区间（立刻生效）
    请求体: {
        "bes_id": "bes1",
        "soc_min": 0.15,
        "soc_max": 0.95
    }
    """
    data = request.get_json(force=True)
    bes_id = data.get("bes_id", "bes1")

    if bes_id not in config.BESS_CONFIG:
        return jsonify({"error": f"未找到储能设备: {bes_id}"}), 404

    cfg = config.BESS_CONFIG[bes_id]
    bs = state.bess_state[bes_id]
    updated = {}

    if "soc_min" in data:
        new_min = float(data["soc_min"])
        if not 0 <= new_min <= 1:
            return jsonify({"error": "soc_min 必须在 [0, 1] 区间内"}), 400
        if "soc_max" in data:
            new_max = float(data["soc_max"])
            if new_min >= new_max:
                return jsonify({"error": "soc_min 必须小于 soc_max"}), 400
        elif new_min >= cfg["soc_max"]:
            return jsonify({"error": f"soc_min ({new_min}) 必须小于当前 soc_max ({cfg['soc_max']})"}), 400
        cfg["soc_min"] = new_min
        updated["soc_min"] = new_min
        if bs.soc < new_min:
            bs.soc = new_min
            updated["soc_adjused_to_min"] = True

    if "soc_max" in data:
        new_max = float(data["soc_max"])
        if not 0 <= new_max <= 1:
            return jsonify({"error": "soc_max 必须在 [0, 1] 区间内"}), 400
        if new_max <= cfg["soc_min"]:
            return jsonify({"error": f"soc_max ({new_max}) 必须大于 soc_min ({cfg['soc_min']})"}), 400
        cfg["soc_max"] = new_max
        updated["soc_max"] = new_max
        if bs.soc > new_max:
            bs.soc = new_max
            updated["soc_adjused_to_max"] = True

    if not updated:
        return jsonify({"error": "未提供有效的SOC配置修改"}), 400

    return jsonify({
        "status": "ok",
        "message": "电池SOC工作区间已更新，立即生效",
        "bes_id": bes_id,
        "updated": updated,
        "current_config": {
            "soc_min": cfg["soc_min"],
            "soc_max": cfg["soc_max"],
            "current_soc": bs.soc,
        },
    })


@app.route("/api/config/all", methods=["GET"])
def get_all_config():
    """查询当前全部配置"""
    return jsonify({
        "status": "ok",
        "config": {
            "photovoltaic": config.PV_CONFIG,
            "wind_turbine": config.WT_CONFIG,
            "diesel_generator": config.DIESEL_CONFIG,
            "bess": config.BESS_CONFIG,
            "grid_tariff": {
                p: {"price": config.GRID_TARIFF[p]["price"], "hours": config.GRID_TARIFF[p]["hours"]}
                for p in ["valley", "flat", "peak"]
            },
            "feed_in_tariff": config.FEED_IN_TARIFF,
        }
    })


@app.route("/api/dr/interruptible-loads", methods=["GET"])
def list_interruptible_loads():
    loads = dr_manager.list_interruptible_loads()
    result = []
    for load in loads:
        result.append({
            "load_id": load.load_id,
            "name": load.name,
            "rated_power_kw": load.rated_power_kw,
            "max_reduction_ratio": load.max_reduction_ratio,
            "max_reduction_kw": load.get_max_reduction_kw(),
            "min_duration_minutes": load.min_duration_minutes,
            "cooldown_minutes": load.cooldown_minutes,
            "unit_cost_yuan_per_kwh": load.unit_cost_yuan_per_kwh,
            "current_reduction_kw": load.current_reduction_kw,
            "created_at": load.created_at.isoformat(),
        })
    return jsonify({"status": "ok", "loads": result, "total": len(result)})


@app.route("/api/dr/interruptible-loads", methods=["POST"])
def add_interruptible_load():
    data = request.get_json(force=True)
    required = ["name", "rated_power_kw", "max_reduction_ratio",
                "min_duration_minutes", "cooldown_minutes", "unit_cost_yuan_per_kwh"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    try:
        load = dr_manager.add_interruptible_load(data)
        return jsonify({
            "status": "ok",
            "message": "可中断负荷已添加",
            "load": {
                "load_id": load.load_id,
                "name": load.name,
                "rated_power_kw": load.rated_power_kw,
                "max_reduction_ratio": load.max_reduction_ratio,
                "min_duration_minutes": load.min_duration_minutes,
                "cooldown_minutes": load.cooldown_minutes,
                "unit_cost_yuan_per_kwh": load.unit_cost_yuan_per_kwh,
            }
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/dr/interruptible-loads/<load_id>", methods=["GET"])
def get_interruptible_load(load_id):
    load = dr_manager.get_interruptible_load(load_id)
    if not load:
        return jsonify({"error": "可中断负荷不存在"}), 404
    return jsonify({
        "status": "ok",
        "load": {
            "load_id": load.load_id,
            "name": load.name,
            "rated_power_kw": load.rated_power_kw,
            "max_reduction_ratio": load.max_reduction_ratio,
            "max_reduction_kw": load.get_max_reduction_kw(),
            "min_duration_minutes": load.min_duration_minutes,
            "cooldown_minutes": load.cooldown_minutes,
            "unit_cost_yuan_per_kwh": load.unit_cost_yuan_per_kwh,
            "current_reduction_kw": load.current_reduction_kw,
            "can_reduce_now": load.can_reduce(datetime.now()),
        }
    })


@app.route("/api/dr/interruptible-loads/<load_id>", methods=["PUT"])
def update_interruptible_load(load_id):
    data = request.get_json(force=True) or {}
    load = dr_manager.update_interruptible_load(load_id, data)
    if not load:
        return jsonify({"error": "可中断负荷不存在"}), 404
    return jsonify({
        "status": "ok",
        "message": "可中断负荷已更新",
        "load": {
            "load_id": load.load_id,
            "name": load.name,
            "rated_power_kw": load.rated_power_kw,
            "max_reduction_ratio": load.max_reduction_ratio,
            "min_duration_minutes": load.min_duration_minutes,
            "cooldown_minutes": load.cooldown_minutes,
            "unit_cost_yuan_per_kwh": load.unit_cost_yuan_per_kwh,
        }
    })


@app.route("/api/dr/interruptible-loads/<load_id>", methods=["DELETE"])
def delete_interruptible_load(load_id):
    success = dr_manager.delete_interruptible_load(load_id)
    if not success:
        return jsonify({"error": "可中断负荷不存在"}), 404
    return jsonify({"status": "ok", "message": "可中断负荷已删除", "load_id": load_id})


@app.route("/api/dr/events", methods=["POST"])
def receive_dr_event():
    data = request.get_json(force=True)
    required = ["start_time", "end_time", "target_load_kw",
                "subsidy_unit_price", "penalty_unit_price"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    try:
        event = dr_manager.receive_event(data)
        plan = dr_manager.generate_response_plan(event.event_id)
        return jsonify({
            "status": "ok",
            "message": "需求响应事件已接收，已生成响应方案",
            "event": {
                "event_id": event.event_id,
                "event_no": event.event_no,
                "start_time": event.start_time.isoformat(),
                "end_time": event.end_time.isoformat(),
                "target_load_kw": event.target_load_kw,
                "subsidy_unit_price": event.subsidy_unit_price,
                "penalty_unit_price": event.penalty_unit_price,
                "status": event.status,
                "received_at": event.received_at.isoformat(),
            },
            "plan": {
                "plan_id": plan.plan_id if plan else None,
                "total_reduction_target_kw": plan.total_reduction_target_kw if plan else 0,
                "is_partial_response": plan.is_partial_response if plan else False,
                "expected_gap_kw": plan.expected_gap_kw if plan else 0,
                "notes": plan.notes if plan else [],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/dr/events", methods=["GET"])
def list_dr_events():
    status = request.args.get("status")
    events = dr_manager.list_events(status)
    result = []
    for event in events:
        result.append({
            "event_id": event.event_id,
            "event_no": event.event_no,
            "start_time": event.start_time.isoformat(),
            "end_time": event.end_time.isoformat(),
            "target_load_kw": event.target_load_kw,
            "subsidy_unit_price": event.subsidy_unit_price,
            "penalty_unit_price": event.penalty_unit_price,
            "status": event.status,
            "status_chinese": _get_event_status_chinese(event.status),
            "received_at": event.received_at.isoformat(),
            "confirmed_at": event.confirmed_at.isoformat() if event.confirmed_at else None,
            "finished_at": event.finished_at.isoformat() if event.finished_at else None,
            "early_terminated": event.early_terminated,
        })
    return jsonify({"status": "ok", "events": result, "total": len(result)})


def _get_event_status_chinese(status):
    status_map = {
        "pending": "待响应",
        "confirmed": "已确认",
        "active": "响应中",
        "finished": "已结束",
    }
    return status_map.get(status, status)


@app.route("/api/dr/events/<event_id>", methods=["GET"])
def get_dr_event(event_id):
    event = dr_manager.get_event(event_id)
    if not event:
        return jsonify({"error": "需求响应事件不存在"}), 404
    return jsonify({
        "status": "ok",
        "event": {
            "event_id": event.event_id,
            "event_no": event.event_no,
            "start_time": event.start_time.isoformat(),
            "end_time": event.end_time.isoformat(),
            "target_load_kw": event.target_load_kw,
            "subsidy_unit_price": event.subsidy_unit_price,
            "penalty_unit_price": event.penalty_unit_price,
            "status": event.status,
            "status_chinese": _get_event_status_chinese(event.status),
            "received_at": event.received_at.isoformat(),
            "confirmed_at": event.confirmed_at.isoformat() if event.confirmed_at else None,
            "finished_at": event.finished_at.isoformat() if event.finished_at else None,
            "early_terminated": event.early_terminated,
            "termination_reason": event.termination_reason,
            "current_plan_id": event.current_plan_id,
            "settlement_report_id": event.settlement_report_id,
        }
    })


@app.route("/api/dr/events/<event_id>/plan", methods=["GET"])
def get_dr_response_plan(event_id):
    event = dr_manager.get_event(event_id)
    if not event:
        return jsonify({"error": "需求响应事件不存在"}), 404
    if not event.current_plan_id:
        return jsonify({"error": "该事件暂无响应方案"}), 404

    plan = dr_manager.get_plan(event.current_plan_id)
    if not plan:
        return jsonify({"error": "响应方案不存在"}), 404

    schedule = []
    for period in plan.schedule:
        schedule.append({
            "period_start": period.period_start.isoformat(),
            "period_end": period.period_end.isoformat(),
            "load_reductions": period.load_reductions,
            "battery_discharge_kw": period.battery_discharge_kw,
            "total_reduction_kw": period.total_reduction_kw,
            "target_load_kw": period.target_load_kw,
            "estimated_load_kw": period.estimated_load_kw,
        })

    return jsonify({
        "status": "ok",
        "plan": {
            "plan_id": plan.plan_id,
            "event_id": plan.event_id,
            "generated_at": plan.generated_at.isoformat(),
            "status": plan.status,
            "total_reduction_target_kw": plan.total_reduction_target_kw,
            "is_partial_response": plan.is_partial_response,
            "expected_gap_kw": plan.expected_gap_kw,
            "schedule_count": len(plan.schedule),
            "notes": plan.notes,
            "schedule": schedule,
        }
    })


@app.route("/api/dr/events/<event_id>/confirm", methods=["POST"])
def confirm_dr_plan(event_id):
    event = dr_manager.get_event(event_id)
    if not event:
        return jsonify({"error": "需求响应事件不存在"}), 404

    if not event.current_plan_id:
        return jsonify({"error": "该事件暂无响应方案，无法确认"}), 400

    success = dr_manager.confirm_plan(event_id)
    if not success:
        return jsonify({"error": "确认失败，事件状态不正确或无方案"}), 400

    return jsonify({
        "status": "ok",
        "message": "响应方案已确认，等待事件开始时间自动生效",
        "event_id": event_id,
        "event_status": "confirmed",
    })


@app.route("/api/dr/events/<event_id>/records", methods=["GET"])
def get_dr_execution_records(event_id):
    event = dr_manager.get_event(event_id)
    if not event:
        return jsonify({"error": "需求响应事件不存在"}), 404

    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit 和 offset 必须是整数"}), 400

    records = dr_manager.get_event_execution_records(event_id)
    total = len(records)
    sliced = records[-limit - offset: len(records) - offset] if offset > 0 else records[-limit:]
    sliced = list(reversed(sliced))

    result = []
    for rec in sliced:
        result.append({
            "record_id": rec.record_id,
            "timestamp": rec.timestamp.isoformat(),
            "actual_load_kw": rec.actual_load_kw,
            "target_load_kw": rec.target_load_kw,
            "total_reduction_kw": rec.total_reduction_kw,
            "load_reductions": rec.load_reductions,
            "battery_discharge_kw": rec.battery_discharge_kw,
            "is_compliant": rec.is_compliant,
            "gap_kw": rec.gap_kw,
        })

    return jsonify({
        "status": "ok",
        "event_id": event_id,
        "total": total,
        "returned": len(result),
        "records": result,
    })


@app.route("/api/dr/events/<event_id>/settlement", methods=["GET"])
def get_dr_settlement(event_id):
    event = dr_manager.get_event(event_id)
    if not event:
        return jsonify({"error": "需求响应事件不存在"}), 404

    report = dr_manager.get_settlement_report(event_id)
    if not report:
        return jsonify({"error": "该事件尚无结算报告"}), 404

    settlement_type_chinese = {
        "full": "全额补贴",
        "partial": "部分补贴(80%)",
        "penalty": "考核罚款",
        "none": "无",
    }

    return jsonify({
        "status": "ok",
        "settlement": {
            "report_id": report.report_id,
            "event_id": report.event_id,
            "generated_at": report.generated_at.isoformat(),
            "start_time": report.start_time.isoformat(),
            "end_time": report.end_time.isoformat(),
            "total_periods": report.total_periods,
            "compliant_periods": report.compliant_periods,
            "compliance_rate": round(report.compliance_rate * 100, 2),
            "compliance_rate_percent": f"{report.compliance_rate * 100:.2f}%",
            "total_reduction_kwh": round(report.total_reduction_kwh, 4),
            "total_gap_kwh": round(report.total_gap_kwh, 4),
            "subsidy_unit_price": report.subsidy_unit_price,
            "penalty_unit_price": report.penalty_unit_price,
            "subsidy_amount": round(report.subsidy_amount, 2),
            "penalty_amount": round(report.penalty_amount, 2),
            "net_amount": round(report.net_amount, 2),
            "settlement_type": report.settlement_type,
            "settlement_type_chinese": settlement_type_chinese.get(report.settlement_type, report.settlement_type),
        }
    })


@app.route("/api/dr/events/<event_id>/terminate", methods=["POST"])
def terminate_dr_event(event_id):
    data = request.get_json(force=True) or {}
    reason = data.get("reason", "手动中止")

    report = dr_manager.terminate_event_early(event_id, reason)
    if not report:
        return jsonify({"error": "事件不存在或未在执行中，无法中止"}), 400

    return jsonify({
        "status": "ok",
        "message": "需求响应事件已提前中止",
        "event_id": event_id,
        "termination_reason": reason,
        "settlement": {
            "compliance_rate": f"{report.compliance_rate * 100:.2f}%",
            "subsidy_amount": round(report.subsidy_amount, 2),
            "penalty_amount": round(report.penalty_amount, 2),
            "net_amount": round(report.net_amount, 2),
        }
    })


@app.route("/api/dr/stats", methods=["GET"])
def get_dr_stats():
    stats = dr_manager.get_accumulated_stats()
    return jsonify({
        "status": "ok",
        "stats": {
            "total_events": stats["total_events"],
            "finished_events": stats["finished_events"],
            "active_events": stats["active_events"],
            "pending_events": stats["pending_events"],
            "total_subsidy": stats["total_subsidy"],
            "total_penalty": stats["total_penalty"],
            "net_income": stats["net_income"],
            "total_reduction_kwh": stats["total_reduction_kwh"],
        },
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/dr/current", methods=["GET"])
def get_current_dr_status():
    now = datetime.now()
    dr_info = dr_manager.get_current_reduction(now)
    active_event = dr_manager.get_active_event(now)

    load_details = []
    for load_id, reduction_kw in dr_info.get("load_reductions", {}).items():
        load = dr_manager.get_interruptible_load(load_id)
        load_details.append({
            "load_id": load_id,
            "name": load.name if load else load_id,
            "reduction_kw": reduction_kw,
        })

    all_active_events = [e for e in dr_manager.events.values() if e.status == "active"]
    active_event_details = []
    for e in all_active_events:
        active_event_details.append({
            "event_id": e.event_id,
            "event_no": e.event_no,
            "start_time": e.start_time.isoformat(),
            "end_time": e.end_time.isoformat(),
            "target_load_kw": e.target_load_kw,
            "in_time_window": e.start_time <= now <= e.end_time,
        })

    return jsonify({
        "status": "ok",
        "current_dr": {
            "active": dr_info.get("active", False),
            "event_id": dr_info.get("event_id"),
            "event_no": active_event.event_no if active_event else None,
            "target_load_kw": dr_info.get("target_load_kw", 0),
            "current_load_kw": dr_manager.get_current_load_kw(),
            "total_reduction_kw": sum(dr_info.get("load_reductions", {}).values()) + dr_info.get("battery_discharge_kw", 0),
            "load_reductions": load_details,
            "battery_discharge_kw": dr_info.get("battery_discharge_kw", 0),
            "debug_reason": dr_info.get("debug_reason"),
        },
        "debug": {
            "now": now.isoformat(),
            "all_active_events": active_event_details,
            "active_event_count": len(all_active_events),
        },
        "query_time": now.isoformat(),
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    now = datetime.now()
    return jsonify({
        "status": "ok",
        "service": "电力微网调度与储能管理服务",
        "time": now.isoformat(),
        "sources_reported": _get_awaiting(),
        "dispatch_count": len(state.dispatch_history),
        "alert_count": len(state.alerts),
        "dr_event_count": len(dr_manager.events),
        "dr_active_event": dr_manager.get_active_event(now) is not None,
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "接口不存在", "available_endpoints": _list_endpoints()}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "服务器内部错误", "detail": str(e)}), 500


def _list_endpoints():
    return [
        "POST /api/source/report - 发电源出力上报",
        "POST /api/load/report - 负荷数据上报",
        "POST /api/dispatch/trigger - 手动触发调度",
        "GET /api/status/sources - 发电源状态",
        "GET /api/status/bess - 电池状态",
        "GET /api/battery/health - 电池健康报告",
        "POST /api/battery/health/reset-baseline - 重置电池内阻基线",
        "GET /api/status/tariff - 电价信息",
        "GET /api/status/load - 负荷状态",
        "GET /api/dispatch/history - 调度历史",
        "GET /api/stats/accumulated - 累计统计(含储能套利)",
        "GET /api/storage/plan - 查询当前储能计划",
        "POST /api/storage/plan/regenerate - 手动重新生成储能计划",
        "PUT /api/storage/plan/generation-time - 修改计划生成时刻",
        "GET /api/storage/arbitrage/stats - 查询储能套利统计",
        "GET /api/alerts - 告警记录",
        "PUT /api/config/tariff - 修改电价配置",
        "PUT /api/config/bess_soc - 修改SOC区间",
        "GET /api/config/all - 查看全部配置",
        "GET /api/dr/current - 当前需求响应状态",
        "GET /api/dr/stats - 需求响应累计统计",
        "GET /api/dr/interruptible-loads - 可中断负荷列表",
        "POST /api/dr/interruptible-loads - 添加可中断负荷",
        "GET /api/dr/interruptible-loads/<id> - 查询可中断负荷详情",
        "PUT /api/dr/interruptible-loads/<id> - 更新可中断负荷",
        "DELETE /api/dr/interruptible-loads/<id> - 删除可中断负荷",
        "POST /api/dr/events - 接收需求响应事件",
        "GET /api/dr/events - 需求响应事件列表(支持status筛选)",
        "GET /api/dr/events/<id> - 查询事件详情",
        "GET /api/dr/events/<id>/plan - 查询响应方案",
        "POST /api/dr/events/<id>/confirm - 确认响应方案",
        "GET /api/dr/events/<id>/records - 查询执行记录",
        "GET /api/dr/events/<id>/settlement - 查询结算报告",
        "POST /api/dr/events/<id>/terminate - 手动中止事件",
        "GET /api/health - 健康检查",
    ]


if __name__ == "__main__":
    print("=" * 60)
    print("电力微网调度与储能管理服务启动中...")
    print("=" * 60)
    print(f"光伏阵列: {list(config.PV_CONFIG.keys())}")
    print(f"风力发电机: {list(config.WT_CONFIG.keys())}")
    print(f"柴油发电机: {list(config.DIESEL_CONFIG.keys())}")
    print(f"电池储能: {list(config.BESS_CONFIG.keys())}")
    print(f"初始电池SOC: {config.BESS_CONFIG['bes1']['initial_soc'] * 100:.0f}%")
    print("-" * 60)
    print("需求响应模块: 已启用")
    print(f"可中断负荷数量: {len(dr_manager.interruptible_loads)}")
    for load in dr_manager.list_interruptible_loads():
        print(f"  - {load.name}: {load.rated_power_kw}kW (可削{load.max_reduction_ratio*100:.0f}%)")
    print("=" * 60)
    print("服务地址: http://127.0.0.1:5001")
    print("健康检查: http://127.0.0.1:5001/api/health")
    print("需求响应API:")
    print("  GET  /api/dr/current - 当前需求响应状态")
    print("  GET  /api/dr/stats - 累计统计")
    print("  GET  /api/dr/interruptible-loads - 可中断负荷列表")
    print("  POST /api/dr/events - 接收需求响应事件")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5001, debug=False)
