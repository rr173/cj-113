from datetime import datetime
from flask import Flask, request, jsonify
from dataclasses import asdict

import config
from models import MicrogridState, SourceReport, LoadReport, SimulationStatus
from dispatcher import DispatchEngine
from simulation import SimulationEngine
from demand_response import DemandResponseManager
from price_forecast import PriceForecastManager

app = Flask(__name__)

state = MicrogridState()
dr_manager = DemandResponseManager(state)
price_forecast_manager = PriceForecastManager(state)
engine = DispatchEngine(state, dr_manager, price_forecast_manager)
sim_engine = SimulationEngine(state)


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


@app.route("/api/source/health", methods=["GET"])
def get_sources_health():
    """查询所有发电源的健康评分和状态"""
    result = {}
    for health_key, hs in state.source_health.items():
        status = state.get_source_health_status(health_key)
        if status:
            result[health_key] = status
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "health": result,
    })


@app.route("/api/source/health/<source_type>/<source_id>", methods=["GET"])
def get_source_health_detail(source_type, source_id):
    """查询单个发电源的健康评分"""
    valid_types = {"pv", "wt", "diesel"}
    if source_type not in valid_types:
        return jsonify({"error": f"source_type 必须是 {valid_types} 之一"}), 400
    health_key = f"{source_type}:{source_id}"
    status = state.get_source_health_status(health_key)
    if status is None:
        return jsonify({"error": f"未找到发电源: {health_key}"}), 404
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "health": status,
    })


@app.route("/api/source/health/<source_type>/<source_id>/history", methods=["GET"])
def get_source_health_history(source_type, source_id):
    """查询某个源的历史评分趋势（最近50个点）"""
    valid_types = {"pv", "wt", "diesel"}
    if source_type not in valid_types:
        return jsonify({"error": f"source_type 必须是 {valid_types} 之一"}), 400
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    health_key = f"{source_type}:{source_id}"
    history = state.get_source_health_history(health_key, limit)
    if not history and health_key not in state.source_health:
        return jsonify({"error": f"未找到发电源: {health_key}"}), 404
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "source_type": source_type,
        "source_id": source_id,
        "total_points": len(history),
        "history": history,
    })


@app.route("/api/source/maintenance", methods=["POST"])
def set_source_maintenance():
    """
    手动标记某个源为维护状态（维护期间不计入健康评分也不触发预警）
    请求体: {"source_type": "pv", "source_id": "pv1", "in_maintenance": true}
    """
    data = request.get_json(force=True)
    required = ["source_type", "source_id", "in_maintenance"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400
    valid_types = {"pv", "wt", "diesel"}
    if data["source_type"] not in valid_types:
        return jsonify({"error": f"source_type 必须是 {valid_types} 之一"}), 400
    health_key = f"{data['source_type']}:{data['source_id']}"
    success = state.set_source_maintenance(health_key, bool(data["in_maintenance"]))
    if not success:
        return jsonify({"error": f"未找到发电源: {health_key}"}), 404
    return jsonify({
        "status": "ok",
        "message": f"发电源 {health_key} 维护状态已设置为 {data['in_maintenance']}",
        "health": state.get_source_health_status(health_key),
    })


@app.route("/api/backup-plans", methods=["GET"])
def get_backup_plans():
    """查询当前生效的备用预案列表"""
    plans = state.get_active_backup_plans()
    result = []
    for p in plans:
        result.append({
            "plan_id": p.plan_id,
            "source_type": p.source_type,
            "source_id": p.source_id,
            "generated_at": p.generated_at.isoformat(),
            "can_cover": p.can_cover,
            "can_cover_chinese": "可应对" if p.can_cover else "有缺口",
            "gap_kw": p.gap_kw,
            "load_kw": p.load_kw,
            "lost_capacity_kw": p.lost_capacity_kw,
            "suggestions": p.suggestions,
            "alternative_sources": p.alternative_sources,
        })
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(result),
        "plans": result,
    })


@app.route("/api/backup-plans/<source_type>/<source_id>", methods=["GET"])
def get_backup_plan_for_source(source_type, source_id):
    """查询某个源的备用预案"""
    valid_types = {"pv", "wt", "diesel"}
    if source_type not in valid_types:
        return jsonify({"error": f"source_type 必须是 {valid_types} 之一"}), 400
    health_key = f"{source_type}:{source_id}"
    p = state.get_backup_plan_for_source(health_key)
    if p is None:
        return jsonify({
            "status": "ok",
            "message": f"发电源 {health_key} 当前无生效预案",
            "plan": None,
        })
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "plan": {
            "plan_id": p.plan_id,
            "source_type": p.source_type,
            "source_id": p.source_id,
            "generated_at": p.generated_at.isoformat(),
            "can_cover": p.can_cover,
            "can_cover_chinese": "可应对" if p.can_cover else "有缺口",
            "gap_kw": p.gap_kw,
            "load_kw": p.load_kw,
            "lost_capacity_kw": p.lost_capacity_kw,
            "suggestions": p.suggestions,
            "alternative_sources": p.alternative_sources,
        },
    })


@app.route("/api/fault-events", methods=["GET"])
def get_fault_events():
    """
    查询故障事件记录
    参数: limit (可选，默认100)
    """
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    events = state.get_fault_events(limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(state.fault_events),
        "returned": len(events),
        "events": events,
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


@app.route("/api/simulation/scenarios", methods=["POST"])
def create_scenario():
    """
    创建仿真场景
    请求体: {
        "name": "阴天场景",
        "description": "光伏出力仅为额定20%",
        "duration_hours": 24,
        "time_step_minutes": 1,
        "pv_series": {
            "pv1": {
                "segments": [
                    {"start_minute": 480, "end_minute": 720, "value_kw": 20},
                    {"start_minute": 720, "end_minute": 840, "value_kw": 30}
                ]
            }
        },
        "load_series": {
            "segments": [
                {"start_minute": 0, "end_minute": 1440, "value_kw": 300}
            ]
        },
        "initial_soc_override": {"bes1": 0.6}
    }
    """
    data = request.get_json(force=True) or {}
    if "name" not in data:
        return jsonify({"error": "缺少必填字段: name"}), 400

    scenario = sim_engine.create_scenario(data)
    return jsonify({
        "status": "ok",
        "message": "场景创建成功",
        "scenario": scenario.to_dict(),
    })


@app.route("/api/simulation/scenarios", methods=["GET"])
def list_scenarios():
    """查询仿真场景列表"""
    scenarios = sim_engine.list_scenarios()
    return jsonify({
        "status": "ok",
        "total": len(scenarios),
        "scenarios": scenarios,
    })


@app.route("/api/simulation/scenarios/<scenario_id>", methods=["GET"])
def get_scenario_detail(scenario_id):
    """查询仿真场景详情"""
    scenario = sim_engine.get_scenario(scenario_id)
    if scenario is None:
        return jsonify({"error": f"未找到场景: {scenario_id}"}), 404
    return jsonify({
        "status": "ok",
        "scenario": scenario.to_dict(),
    })


@app.route("/api/simulation/scenarios/<scenario_id>", methods=["PUT"])
def update_scenario(scenario_id):
    """更新仿真场景"""
    data = request.get_json(force=True) or {}
    scenario = sim_engine.update_scenario(scenario_id, data)
    if scenario is None:
        return jsonify({"error": f"未找到场景: {scenario_id}"}), 404
    return jsonify({
        "status": "ok",
        "message": "场景已更新",
        "scenario": scenario.to_dict(),
    })


@app.route("/api/simulation/scenarios/<scenario_id>/copy", methods=["POST"])
def copy_scenario(scenario_id):
    """
    复制仿真场景
    请求体: {"new_name": "阴天场景(修改版)"} 可选
    """
    data = request.get_json(force=True) or {}
    new_name = data.get("new_name")
    new_scenario = sim_engine.copy_scenario(scenario_id, new_name)
    if new_scenario is None:
        return jsonify({"error": f"未找到源场景: {scenario_id}"}), 404
    return jsonify({
        "status": "ok",
        "message": "场景复制成功",
        "scenario": new_scenario.to_dict(),
    })


@app.route("/api/simulation/scenarios/<scenario_id>", methods=["DELETE"])
def delete_scenario(scenario_id):
    """删除仿真场景"""
    success = sim_engine.delete_scenario(scenario_id)
    if not success:
        return jsonify({"error": f"未找到场景: {scenario_id}"}), 404
    return jsonify({
        "status": "ok",
        "message": f"场景 {scenario_id} 已删除",
    })


@app.route("/api/simulation/run", methods=["POST"])
def run_simulation():
    """
    运行仿真
    请求体: {"scenario_id": "SCEN-XXXXXX"}
    """
    data = request.get_json(force=True) or {}
    scenario_id = data.get("scenario_id")
    if not scenario_id:
        return jsonify({"error": "缺少必填字段: scenario_id"}), 400

    report = sim_engine.run_simulation(scenario_id)
    if report is None:
        return jsonify({"error": f"未找到场景: {scenario_id}"}), 404

    return jsonify({
        "status": "ok",
        "message": "仿真执行完成" if report.status == SimulationStatus.COMPLETED else "仿真执行失败",
        "simulation": report.to_dict(include_steps=False),
    })


@app.route("/api/simulation/simulations", methods=["GET"])
def list_simulations():
    """
    查询仿真任务列表
    参数: scenario_id (可选，按场景过滤)
    """
    scenario_id = request.args.get("scenario_id")
    simulations = sim_engine.list_simulations(scenario_id)
    return jsonify({
        "status": "ok",
        "total": len(simulations),
        "simulations": simulations,
    })


@app.route("/api/simulation/simulations/<sim_id>", methods=["GET"])
def get_simulation_status(sim_id):
    """查询仿真状态/结果"""
    report = sim_engine.get_simulation(sim_id)
    if report is None:
        return jsonify({"error": f"未找到仿真任务: {sim_id}"}), 404
    return jsonify({
        "status": "ok",
        "simulation": report.to_dict(include_steps=False),
    })


@app.route("/api/simulation/simulations/<sim_id>/report", methods=["GET"])
def get_simulation_report(sim_id):
    """查询仿真报告详情"""
    report = sim_engine.get_simulation(sim_id)
    if report is None:
        return jsonify({"error": f"未找到仿真任务: {sim_id}"}), 404
    return jsonify({
        "status": "ok",
        "report": report.to_dict(include_steps=False),
    })


@app.route("/api/simulation/simulations/<sim_id>/steps", methods=["GET"])
def get_simulation_steps(sim_id):
    """
    查询仿真逐步记录
    参数: limit (可选), offset (可选)
    """
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit 和 offset 必须是整数"}), 400

    report = sim_engine.get_simulation(sim_id)
    if report is None:
        return jsonify({"error": f"未找到仿真任务: {sim_id}"}), 404

    all_steps = report.step_records
    total = len(all_steps)
    sliced = all_steps[offset: offset + limit]

    steps_data = [
        {
            "step_index": s.step_index,
            "simulation_time": s.simulation_time.isoformat(),
            "scenario_minute": s.scenario_minute,
            "pv_output": s.pv_output,
            "wt_output": s.wt_output,
            "diesel_output": s.diesel_output,
            "bess_soc_before_percent": {k: round(v * 100, 2) for k, v in s.bess_soc_before.items()},
            "bess_soc_after_percent": {k: round(v * 100, 2) for k, v in s.bess_soc_after.items()},
            "bess_charge_kw": s.bess_charge_kw,
            "bess_discharge_kw": s.bess_discharge_kw,
            "grid_import_kw": s.grid_import_kw,
            "grid_export_kw": s.grid_export_kw,
            "load_served_kw": s.load_served_kw,
            "load_shed_kw": s.load_shed_kw,
            "step_cost": round(s.step_cost, 4),
            "tariff_period": s.tariff_period,
            "notes": s.notes,
        }
        for s in sliced
    ]

    return jsonify({
        "status": "ok",
        "simulation_id": sim_id,
        "total_steps": total,
        "returned": len(steps_data),
        "limit": limit,
        "offset": offset,
        "steps": steps_data,
    })


@app.route("/api/simulation/compare", methods=["GET"])
def compare_simulations():
    """
    对比两个仿真结果
    参数: sim_a_id, sim_b_id
    """
    sim_a_id = request.args.get("sim_a_id")
    sim_b_id = request.args.get("sim_b_id")

    if not sim_a_id or not sim_b_id:
        return jsonify({"error": "缺少必填参数: sim_a_id, sim_b_id"}), 400

    comparison = sim_engine.compare_simulations(sim_a_id, sim_b_id)
    if comparison is None:
        sim_a = sim_engine.get_simulation(sim_a_id)
        sim_b = sim_engine.get_simulation(sim_b_id)
        if sim_a is None:
            return jsonify({"error": f"未找到仿真任务: {sim_a_id}"}), 404
        if sim_b is None:
            return jsonify({"error": f"未找到仿真任务: {sim_b_id}"}), 404
        return jsonify({"error": "两个仿真都需要已完成状态才能对比"}), 400

    return jsonify({
        "status": "ok",
        "comparison": comparison.to_dict(),
    })


@app.route("/api/price-forecast/submit", methods=["POST"])
def submit_price_forecast():
    """
    提交次日电价预告
    请求体: {
        "prices": [0.35, 0.35, ...],  // 24个浮点数，单位元/kWh
        "forecast_date": "2024-01-01"  // 可选，默认次日
    }
    """
    data = request.get_json(force=True) or {}

    if "prices" not in data:
        return jsonify({"error": "缺少必填字段: prices"}), 400

    prices = data["prices"]
    if not isinstance(prices, list) or len(prices) != 24:
        return jsonify({"error": "prices 必须是包含24个浮点数的列表"}), 400

    try:
        prices = [float(p) for p in prices]
    except (ValueError, TypeError):
        return jsonify({"error": "prices 中的值必须是数字"}), 400

    forecast_date = data.get("forecast_date")
    if forecast_date is not None:
        try:
            datetime.strptime(forecast_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "forecast_date 格式必须为 YYYY-MM-DD"}), 400

    try:
        result = price_forecast_manager.submit_forecast(prices, forecast_date)
        return jsonify({
            "status": "ok",
            "message": "电价预告已提交，策略建议已生成（待激活状态）",
            "forecast": result["forecast"].to_dict(),
            "comparison": result["comparison"].to_dict(),
            "strategy": result["strategy"].to_dict(),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/price-forecast/current", methods=["GET"])
def get_current_price_forecast():
    """
    查询当前生效的电价预告和对比分析结果
    """
    forecast = price_forecast_manager.get_active_forecast()
    strategy = price_forecast_manager.get_active_strategy()

    result = {
        "has_active_forecast": forecast is not None,
    }

    if forecast:
        comparison = price_forecast_manager.get_comparison(forecast.forecast_id)
        result["forecast"] = forecast.to_dict()
        result["comparison"] = comparison.to_dict() if comparison else None
        result["strategy"] = strategy.to_dict() if strategy else None
    else:
        result["message"] = "当前无生效的电价预告策略"

    return jsonify({
        "status": "ok",
        "data": result,
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/price-forecast/strategy", methods=["GET"])
def get_strategy_detail():
    """
    查询策略建议详情（各时段建议动作）
    参数: forecast_id (可选，默认查当前激活的)
    """
    forecast_id = request.args.get("forecast_id")

    if forecast_id:
        strategy = price_forecast_manager.get_strategy_by_forecast(forecast_id)
        forecast = price_forecast_manager.get_forecast(forecast_id)
    else:
        strategy = price_forecast_manager.get_active_strategy()
        forecast = price_forecast_manager.get_active_forecast()

    if not strategy:
        return jsonify({"error": "未找到对应的策略建议"}), 404

    return jsonify({
        "status": "ok",
        "forecast": forecast.to_dict() if forecast else None,
        "strategy": strategy.to_dict(),
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/price-forecast/activate", methods=["POST"])
def activate_strategy():
    """
    激活电价策略
    请求体: {"forecast_id": "FCST-000001"}
    """
    data = request.get_json(force=True) or {}
    forecast_id = data.get("forecast_id")

    if not forecast_id:
        return jsonify({"error": "缺少必填字段: forecast_id"}), 400

    success = price_forecast_manager.activate_strategy(forecast_id)
    if not success:
        return jsonify({"error": "激活失败，电价预告不存在或状态不正确"}), 400

    forecast = price_forecast_manager.get_forecast(forecast_id)
    strategy = price_forecast_manager.get_strategy_by_forecast(forecast_id)

    return jsonify({
        "status": "ok",
        "message": "电价策略已激活，将覆盖当天的储能计划模式",
        "forecast": forecast.to_dict() if forecast else None,
        "strategy": strategy.to_dict() if strategy else None,
        "activated_at": datetime.now().isoformat(),
    })


@app.route("/api/price-forecast/deactivate", methods=["POST"])
def deactivate_strategy():
    """
    停用电价策略
    请求体: {"forecast_id": "FCST-000001"}
    """
    data = request.get_json(force=True) or {}
    forecast_id = data.get("forecast_id")

    if not forecast_id:
        return jsonify({"error": "缺少必填字段: forecast_id"}), 400

    success = price_forecast_manager.deactivate_strategy(forecast_id)
    if not success:
        return jsonify({"error": "停用失败，电价预告不存在或状态不正确"}), 400

    return jsonify({
        "status": "ok",
        "message": "电价策略已停用，已恢复固定电价模式",
        "forecast_id": forecast_id,
        "deactivated_at": datetime.now().isoformat(),
    })


@app.route("/api/price-forecast/history", methods=["GET"])
def get_price_forecast_history():
    """
    查询历史电价预告记录
    参数: date (可选，按日期查询), limit (可选，默认30)
    """
    date_str = request.args.get("date")
    limit = request.args.get("limit", 30)

    try:
        limit = int(limit)
    except (ValueError, TypeError):
        return jsonify({"error": "limit 必须是整数"}), 400

    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "date 格式必须为 YYYY-MM-DD"}), 400

        forecast = price_forecast_manager.get_forecast_by_date(date_str)
        if forecast:
            comparison = price_forecast_manager.get_comparison(forecast.forecast_id)
            strategy = price_forecast_manager.get_strategy_by_forecast(forecast.forecast_id)
            return jsonify({
                "status": "ok",
                "total": 1,
                "forecasts": [{
                    "forecast": forecast.to_dict(),
                    "comparison": comparison.to_dict() if comparison else None,
                    "strategy_summary": strategy.summary if strategy else None,
                }],
                "query_time": datetime.now().isoformat(),
            })
        else:
            return jsonify({
                "status": "ok",
                "total": 0,
                "forecasts": [],
                "message": f"日期 {date_str} 无电价预告记录",
                "query_time": datetime.now().isoformat(),
            })
    else:
        forecasts = price_forecast_manager.list_forecasts(limit)
        result = []
        for f in forecasts:
            comparison = price_forecast_manager.get_comparison(f.forecast_id)
            strategy = price_forecast_manager.get_strategy_by_forecast(f.forecast_id)
            result.append({
                "forecast": f.to_dict(),
                "comparison": comparison.to_dict() if comparison else None,
                "strategy_summary": strategy.summary if strategy else None,
            })

        return jsonify({
            "status": "ok",
            "total": len(result),
            "forecasts": result,
            "query_time": datetime.now().isoformat(),
        })


@app.route("/api/price-forecast/stats", methods=["GET"])
def get_strategy_execution_stats():
    """
    查询策略执行效果统计
    参数: start_date (可选), end_date (可选)
    """
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "start_date 格式必须为 YYYY-MM-DD"}), 400

    if end_date:
        try:
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "end_date 格式必须为 YYYY-MM-DD"}), 400

    stats = price_forecast_manager.get_execution_stats(start_date, end_date)

    return jsonify({
        "status": "ok",
        "stats": stats.to_dict(),
        "query_time": datetime.now().isoformat(),
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    active_forecast = price_forecast_manager.get_active_forecast()
    return jsonify({
        "status": "ok",
        "service": "电力微网调度与储能管理服务",
        "time": datetime.now().isoformat(),
        "sources_reported": _get_awaiting(),
        "dispatch_count": len(state.dispatch_history),
        "alert_count": len(state.alerts),
        "backup_plan_count": len(state.backup_plans),
        "fault_event_count": len(state.fault_events),
        "price_forecast_active": active_forecast is not None,
        "active_forecast_id": active_forecast.forecast_id if active_forecast else None,
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
        "GET /api/stats/accumulated - 累计统计",
        "GET /api/storage/plan - 查询储能计划",
        "POST /api/storage/plan/regenerate - 重新生成储能计划",
        "PUT /api/storage/plan/generation-time - 修改储能计划生成时刻",
        "GET /api/storage/arbitrage/stats - 储能套利统计",
        "GET /api/alerts - 告警记录",
        "GET /api/source/health - 所有发电源健康评分",
        "GET /api/source/health/<type>/<id> - 单个发电源健康评分",
        "GET /api/source/health/<type>/<id>/history - 发电源健康评分历史趋势",
        "POST /api/source/maintenance - 设置发电源维护状态",
        "GET /api/backup-plans - 备用预案列表",
        "GET /api/backup-plans/<type>/<id> - 单源备用预案",
        "GET /api/fault-events - 故障事件记录",
        "GET /api/dr/interruptible-loads - 查询可中断负荷列表",
        "POST /api/dr/interruptible-loads - 添加可中断负荷",
        "GET /api/dr/interruptible-loads/<id> - 查询可中断负荷详情",
        "PUT /api/dr/interruptible-loads/<id> - 更新可中断负荷",
        "DELETE /api/dr/interruptible-loads/<id> - 删除可中断负荷",
        "POST /api/dr/events - 接收需求响应事件",
        "GET /api/dr/events - 查询需求响应事件列表",
        "GET /api/dr/events/<id> - 查询需求响应事件详情",
        "GET /api/dr/events/<id>/plan - 查询响应方案",
        "POST /api/dr/events/<id>/confirm - 确认响应方案",
        "GET /api/dr/events/<id>/records - 查询执行记录",
        "GET /api/dr/events/<id>/settlement - 查询结算报告",
        "POST /api/dr/events/<id>/terminate - 提前终止事件",
        "GET /api/dr/stats - 需求响应累计统计",
        "GET /api/dr/current - 当前需求响应状态",
        "PUT /api/config/tariff - 修改电价配置",
        "PUT /api/config/bess_soc - 修改SOC区间",
        "GET /api/config/all - 查看全部配置",
        "GET /api/health - 健康检查",
        "POST /api/simulation/scenarios - 创建仿真场景",
        "GET /api/simulation/scenarios - 查询仿真场景列表",
        "GET /api/simulation/scenarios/<id> - 查询仿真场景详情",
        "PUT /api/simulation/scenarios/<id> - 更新仿真场景",
        "POST /api/simulation/scenarios/<id>/copy - 复制仿真场景",
        "DELETE /api/simulation/scenarios/<id> - 删除仿真场景",
        "POST /api/simulation/run - 运行仿真",
        "GET /api/simulation/simulations - 查询仿真任务列表",
        "GET /api/simulation/simulations/<id> - 查询仿真状态",
        "GET /api/simulation/simulations/<id>/report - 查询仿真报告",
        "GET /api/simulation/simulations/<id>/steps - 查询仿真逐步记录",
        "GET /api/simulation/compare - 对比两个仿真结果",
        "POST /api/price-forecast/submit - 提交次日电价预告",
        "GET /api/price-forecast/current - 查询当前生效的电价预告",
        "GET /api/price-forecast/strategy - 查询策略建议详情",
        "POST /api/price-forecast/activate - 激活电价策略",
        "POST /api/price-forecast/deactivate - 停用电价策略",
        "GET /api/price-forecast/history - 查询历史电价预告记录",
        "GET /api/price-forecast/stats - 查询策略执行效果统计",
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
    print(f"  - 可中断负荷: {len(dr_manager.interruptible_loads)} 个")
    print("  - 事件接收: POST /api/dr/events")
    print("  - 负荷管理: GET /api/dr/interruptible-loads")
    print("  - 当前状态: GET /api/dr/current")
    print("-" * 60)
    print("多场景仿真与回测引擎: 已启用")
    print("  - 创建场景: POST /api/simulation/scenarios")
    print("  - 运行仿真: POST /api/simulation/run")
    print("  - 对比结果: GET /api/simulation/compare")
    print("-" * 60)
    print("电价预测与购电策略优化模块: 已启用")
    print("  - 提交预告: POST /api/price-forecast/submit")
    print("  - 当前策略: GET /api/price-forecast/current")
    print("  - 策略详情: GET /api/price-forecast/strategy")
    print("  - 激活策略: POST /api/price-forecast/activate")
    print("  - 历史记录: GET /api/price-forecast/history")
    print("  - 效果统计: GET /api/price-forecast/stats")
    print("=" * 60)
    print("服务地址: http://127.0.0.1:5001")
    print("健康检查: http://127.0.0.1:5001/api/health")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5001, debug=False)
