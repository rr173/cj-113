from datetime import datetime, timedelta
from typing import Optional
from flask import Flask, request, jsonify
from dataclasses import asdict

import config
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
from audit import DecisionComparator

app = Flask(__name__)

state = MicrogridState()
engine = DispatchEngine(state)


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
        "group_shed_details": d.group_shed_details,
        "group_restore_details": d.group_restore_details,
    }


def _audit_to_dict_brief(audit):
    return {
        "audit_id": audit.audit_id,
        "dispatch_id": audit.dispatch_id,
        "timestamp": audit.timestamp.isoformat(),
        "total_cost": audit.output_summary.total_cost,
        "load_served_kw": audit.output_summary.load_served_kw,
        "load_shed_kw": audit.output_summary.load_shed_kw,
        "has_load_shed": audit.output_summary.load_shed_kw > 0,
        "has_diesel_start": audit.output_summary.diesel_share_kw > 0,
        "has_anomaly": audit.has_anomaly(),
        "anomaly_count": len(audit.anomalies),
        "anomaly_types": [a.anomaly_type for a in audit.anomalies],
        "tariff_period": audit.input_snapshot.tariff_period,
        "load_kw": audit.input_snapshot.load_kw,
    }


def _audit_to_dict_detail(audit):
    return {
        "audit_id": audit.audit_id,
        "dispatch_id": audit.dispatch_id,
        "timestamp": audit.timestamp.isoformat(),
        "input_snapshot": {
            "pv_output_kw": audit.input_snapshot.pv_output,
            "wt_output_kw": audit.input_snapshot.wt_output,
            "diesel_available": audit.input_snapshot.diesel_available,
            "load_kw": audit.input_snapshot.load_kw,
            "bess_soc": {k: round(v * 100, 2) for k, v in audit.input_snapshot.bess_soc.items()},
            "grid_buy_price": audit.input_snapshot.grid_buy_price,
            "feed_in_price": audit.input_snapshot.feed_in_price,
            "tariff_period": audit.input_snapshot.tariff_period,
            "hour": audit.input_snapshot.hour,
            "storage_strategy_active": audit.input_snapshot.storage_strategy_active,
            "storage_mode": audit.input_snapshot.storage_mode,
            "demand_response_active": audit.input_snapshot.demand_response_active,
            "active_backup_plans": audit.input_snapshot.active_backup_plans,
            "source_health_status": audit.input_snapshot.source_health_status,
        },
        "decision_branches": [
            {
                "branch_name": b.branch_name,
                "decision": b.decision,
                "decision_text": "是" if b.decision else "否",
                "reason": b.reason,
                "details": b.details,
            }
            for b in audit.decision_branches
        ],
        "output_summary": {
            "load_served_kw": audit.output_summary.load_served_kw,
            "load_shed_kw": audit.output_summary.load_shed_kw,
            "load_coverage_ratio": round(audit.output_summary.load_coverage_ratio * 100, 2),
            "total_cost": audit.output_summary.total_cost,
            "pv_share_kw": audit.output_summary.pv_share_kw,
            "wt_share_kw": audit.output_summary.wt_share_kw,
            "diesel_share_kw": audit.output_summary.diesel_share_kw,
            "bess_discharge_kw": audit.output_summary.bess_discharge_kw,
            "grid_import_kw": audit.output_summary.grid_import_kw,
            "grid_export_kw": audit.output_summary.grid_export_kw,
            "cost_breakdown": audit.output_summary.cost_breakdown,
        },
        "anomalies": [
            {
                "anomaly_type": a.anomaly_type,
                "severity": a.severity,
                "severity_level": {"critical": 3, "high": 2, "medium": 1, "low": 0}.get(a.severity, 0),
                "description": a.description,
                "details": a.details,
            }
            for a in audit.anomalies
        ],
        "reasoning_chain": audit.reasoning_chain,
        "has_anomaly": audit.has_anomaly(),
    }


def _parse_bool_param(param_str: Optional[str]) -> Optional[bool]:
    if param_str is None:
        return None
    lower_val = param_str.lower()
    if lower_val in ("true", "1", "yes", "y"):
        return True
    elif lower_val in ("false", "0", "no", "n"):
        return False
    return None


@app.route("/api/audit/logs", methods=["GET"])
def get_audit_logs():
    """
    查询审计日志列表
    参数:
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
      - min_cost: 最低决策成本阈值 (可选)
      - has_load_shed / load_shed: 是否只看甩负荷的 (true/false，可选)
      - has_diesel_start / diesel_start: 是否只看启动柴油机的 (true/false，可选)
      - has_anomaly / anomaly_only: 是否只看异常的 (true/false，可选)
      - limit: 返回数量限制 (默认50)
      - offset: 分页偏移 (默认0)
    """
    try:
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")
        min_cost_str = request.args.get("min_cost")
        has_load_shed_str = request.args.get("has_load_shed") or request.args.get("load_shed")
        has_diesel_start_str = request.args.get("has_diesel_start") or request.args.get("diesel_start")
        has_anomaly_str = request.args.get("has_anomaly") or request.args.get("anomaly_only")
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None
        min_cost = float(min_cost_str) if min_cost_str else None

        has_load_shed = _parse_bool_param(has_load_shed_str)
        has_diesel_start = _parse_bool_param(has_diesel_start_str)
        has_anomaly = _parse_bool_param(has_anomaly_str)

        logs = state.query_audit_logs(
            start_time=start_time,
            end_time=end_time,
            min_cost=min_cost,
            has_load_shed=has_load_shed,
            has_diesel_start=has_diesel_start,
            has_anomaly=has_anomaly,
            limit=limit,
            offset=offset,
        )

        total_filtered = len(state.query_audit_logs(
            start_time=start_time,
            end_time=end_time,
            min_cost=min_cost,
            has_load_shed=has_load_shed,
            has_diesel_start=has_diesel_start,
            has_anomaly=has_anomaly,
            limit=1000000,
            offset=0,
        ))

        return jsonify({
            "status": "ok",
            "total": len(state.audit_logs),
            "total_filtered": total_filtered,
            "returned": len(logs),
            "limit": limit,
            "offset": offset,
            "filters": {
                "start_time": start_time_str,
                "end_time": end_time_str,
                "min_cost": min_cost_str,
                "has_load_shed": has_load_shed,
                "has_diesel_start": has_diesel_start,
                "has_anomaly": has_anomaly,
            },
            "logs": [_audit_to_dict_brief(log) for log in logs],
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/audit/logs/<audit_id>", methods=["GET"])
def get_audit_log_detail(audit_id):
    """
    查询单条审计日志详情（含完整推理链路）
    """
    audit = state.get_audit_log(audit_id)
    if audit is None:
        return jsonify({"error": f"未找到审计日志: {audit_id}"}), 404

    return jsonify({
        "status": "ok",
        "audit": _audit_to_dict_detail(audit),
    })


@app.route("/api/audit/anomalies", methods=["GET"])
def get_anomaly_logs():
    """
    查询异常审计日志列表
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    logs = state.get_anomaly_audit_logs(limit=limit)
    return jsonify({
        "status": "ok",
        "total_anomalies": len([l for l in state.audit_logs if l.has_anomaly()]),
        "total_audits": len(state.audit_logs),
        "returned": len(logs),
        "logs": [_audit_to_dict_brief(log) for log in logs],
    })


@app.route("/api/audit/compare", methods=["GET"])
def compare_audits():
    """
    决策对比接口：对比两条审计日志的输入输出差异
    参数:
      - audit_id1: 第一条审计日志ID
      - audit_id2: 第二条审计日志ID
    """
    audit_id1 = request.args.get("audit_id1")
    audit_id2 = request.args.get("audit_id2")

    if not audit_id1 or not audit_id2:
        return jsonify({"error": "必须提供 audit_id1 和 audit_id2 参数"}), 400

    audit1 = state.get_audit_log(audit_id1)
    audit2 = state.get_audit_log(audit_id2)

    if audit1 is None:
        return jsonify({"error": f"未找到审计日志: {audit_id1}"}), 404
    if audit2 is None:
        return jsonify({"error": f"未找到审计日志: {audit_id2}"}), 404

    comparison = DecisionComparator.compare_audits(audit1, audit2)

    return jsonify({
        "status": "ok",
        "comparison": comparison,
    })


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
    for gid in config.LOAD_GROUP_CONFIG:
        result[f"load_group:{gid}"] = gid in state.load_group_reports
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


@app.route("/api/load/group/report", methods=["POST"])
def report_load_group():
    """
    按群组分别上报负荷实际功率
    请求体: {
        "group_id": "group1" | "group2" | "group3",
        "actual_power_kw": 45.2,
        "auto_dispatch": true (可选，默认true)
    }
    """
    data = request.get_json(force=True)
    required = ["group_id", "actual_power_kw"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    group_id = data["group_id"]
    valid_groups = set(config.LOAD_GROUP_CONFIG.keys())
    if group_id not in valid_groups:
        return jsonify({"error": f"group_id 必须是 {valid_groups} 之一"}), 400

    try:
        state.report_load_group(group_id, float(data["actual_power_kw"]))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try_dispatch = data.get("auto_dispatch", True)
    if try_dispatch and state.all_sources_reported():
        decision = engine.execute()
        return jsonify({
            "status": "ok",
            "message": f"群组[{group_id}]负荷数据已接收，已执行调度决策",
            "group_report": {
                "group_id": group_id,
                "reported_power_kw": state.load_group_state[group_id]["reported_power_kw"],
                "timestamp": state.load_group_state[group_id]["last_report_time"].isoformat(),
            },
            "dispatch_decision": _decision_to_dict(decision),
            "group_status": state.get_load_group_status(group_id),
        })

    return jsonify({
        "status": "ok",
        "message": f"群组[{group_id}]负荷数据已接收，等待其它源/群组上报后执行调度",
        "group_report": {
            "group_id": group_id,
            "reported_power_kw": state.load_group_state[group_id]["reported_power_kw"],
            "timestamp": state.load_group_state[group_id]["last_report_time"].isoformat() if state.load_group_state[group_id]["last_report_time"] else None,
        },
        "awaiting": _get_awaiting(),
        "all_groups_reported": state.all_groups_reported(),
    })


@app.route("/api/load/groups/report", methods=["POST"])
def report_all_load_groups():
    """
    批量上报所有群组的负荷功率
    请求体: {
        "groups": [
            {"group_id": "group1", "actual_power_kw": 48.0},
            {"group_id": "group2", "actual_power_kw": 115.0},
            {"group_id": "group3", "actual_power_kw": 170.0}
        ],
        "auto_dispatch": true (可选，默认true)
    }
    """
    data = request.get_json(force=True)
    if "groups" not in data or not isinstance(data["groups"], list):
        return jsonify({"error": "缺少必填字段: groups (数组形式)"}), 400

    valid_groups = set(config.LOAD_GROUP_CONFIG.keys())
    timestamp = datetime.now()
    reported = []
    errors = []

    from models import LoadGroupReport, LoadReport
    group_reports = {}
    total_load = 0.0

    for item in data["groups"]:
        gid = item.get("group_id")
        kw = item.get("actual_power_kw")
        if gid not in valid_groups:
            errors.append(f"无效的group_id: {gid}")
            continue
        if kw is None:
            errors.append(f"群组{gid}缺少actual_power_kw")
            continue
        kw_f = max(0.0, float(kw))
        gr = LoadGroupReport(group_id=gid, actual_power_kw=kw_f, timestamp=timestamp)
        group_reports[gid] = gr
        total_load += kw_f
        reported.append(gid)
        state.load_group_reports[gid] = gr
        gs = state.load_group_state[gid]
        gs["reported_power_kw"] = kw_f
        gs["last_report_time"] = timestamp

    if errors:
        return jsonify({"error": "; ".join(errors)}), 400

    full_report = LoadReport(load_kw=total_load, timestamp=timestamp, group_reports=group_reports)
    state.report_load(full_report)

    try_dispatch = data.get("auto_dispatch", True)
    if try_dispatch and state.all_sources_reported():
        decision = engine.execute()
        return jsonify({
            "status": "ok",
            "message": "所有群组负荷数据已接收，已执行调度决策",
            "reported_groups": reported,
            "total_load_kw": round(total_load, 2),
            "dispatch_decision": _decision_to_dict(decision),
            "all_groups_status": state.get_load_group_status(),
        })

    return jsonify({
        "status": "ok",
        "message": f"已上报{len(reported)}个群组，总负荷{total_load:.2f}kW，等待其它源上报后执行调度",
        "reported_groups": reported,
        "total_load_kw": round(total_load, 2),
        "awaiting": _get_awaiting(),
    })


@app.route("/api/load/groups/status", methods=["GET"])
def get_load_groups_status():
    """
    查询各群组当前状态
    参数: group_id (可选，指定单个群组)
    """
    group_id = request.args.get("group_id")
    if group_id and group_id not in config.LOAD_GROUP_CONFIG:
        return jsonify({"error": f"未找到负荷群组: {group_id}"}), 404

    result = state.get_load_group_status(group_id)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "group_id": group_id,
        "groups": result,
    })


@app.route("/api/load/groups/config", methods=["PUT"])
def update_load_group_config():
    """
    修改群组配置（额定功率、最大切除比例），修改后立刻影响下次调度决策
    请求体: {
        "group_id": "group2",
        "rated_power_kw": 150.0, (可选)
        "max_shed_ratio": 0.7 (可选，group1不允许>0)
    }
    """
    data = request.get_json(force=True)
    if "group_id" not in data:
        return jsonify({"error": "缺少必填字段: group_id"}), 400

    group_id = data["group_id"]
    if group_id not in config.LOAD_GROUP_CONFIG:
        return jsonify({"error": f"未找到负荷群组: {group_id}"}), 404

    rated_power = data.get("rated_power_kw")
    max_shed_ratio = data.get("max_shed_ratio")
    updated_fields = {}

    if rated_power is not None:
        rated_power = float(rated_power)
        if rated_power < 0:
            return jsonify({"error": "rated_power_kw 不能为负"}), 400
        updated_fields["rated_power_kw"] = rated_power

    if max_shed_ratio is not None:
        max_shed_ratio = float(max_shed_ratio)
        if not (0 <= max_shed_ratio <= 1):
            return jsonify({"error": "max_shed_ratio 必须在 [0, 1] 区间内"}), 400
        if group_id == "group1" and max_shed_ratio > 0:
            return jsonify({"error": "一级(关键)负荷不允许切除，max_shed_ratio必须为0"}), 400
        updated_fields["max_shed_ratio"] = max_shed_ratio

    if not updated_fields:
        return jsonify({"error": "未提供有效的配置修改(rated_power_kw / max_shed_ratio)"}), 400

    success = state.update_load_group_config(
        group_id,
        rated_power_kw=updated_fields.get("rated_power_kw"),
        max_shed_ratio=updated_fields.get("max_shed_ratio"),
    )
    if not success:
        return jsonify({"error": "配置更新失败"}), 500

    return jsonify({
        "status": "ok",
        "message": "群组配置已更新，立即生效（影响下次调度决策）",
        "group_id": group_id,
        "updated": updated_fields,
        "current_config": {
            "rated_power_kw": state.load_group_state[group_id]["rated_power_kw"],
            "max_shed_ratio": state.load_group_state[group_id]["max_shed_ratio"],
            "name": state.load_group_state[group_id]["name"],
        },
    })


@app.route("/api/load/groups/shed-history", methods=["GET"])
def get_load_group_shed_history():
    """
    查询历史切除事件
    参数:
      - group_id: 可选，指定群组
      - limit: 可选，返回条数上限(默认100)
    """
    group_id = request.args.get("group_id")
    if group_id and group_id not in config.LOAD_GROUP_CONFIG:
        return jsonify({"error": f"未找到负荷群组: {group_id}"}), 404
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    events = state.get_load_group_shed_history(group_id, limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "group_id": group_id,
        "total_events": len(state.load_group_shed_events),
        "returned": len(events),
        "active_shed_count": len([e for e in state.load_group_shed_events if e.ended_at is None]),
        "events": events,
    })


@app.route("/api/load/groups/reliability", methods=["GET"])
def get_load_group_reliability():
    """
    查询供电可靠性统计（各群组的供电保障率=正常供电时间/总时间）
    参数:
      - group_id: 可选，指定群组
      - start_time: 可选，统计窗口开始(ISO格式)
      - end_time: 可选，统计窗口结束(ISO格式)
    """
    group_id = request.args.get("group_id")
    if group_id and group_id not in config.LOAD_GROUP_CONFIG:
        return jsonify({"error": f"未找到负荷群组: {group_id}"}), 404

    start_time_str = request.args.get("start_time")
    end_time_str = request.args.get("end_time")
    start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
    end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

    stats = state.get_load_group_reliability_stats(group_id, start_time, end_time)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "group_id": group_id,
        "time_window": {
            "start": start_time_str,
            "end": end_time_str,
        },
        "reliability_stats": stats,
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
            "load_groups": config.LOAD_GROUP_CONFIG,
        }
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "ok",
        "service": "电力微网调度与储能管理服务",
        "time": datetime.now().isoformat(),
        "sources_reported": _get_awaiting(),
        "dispatch_count": len(state.dispatch_history),
        "audit_log_count": len(state.audit_logs),
        "anomaly_count": len([l for l in state.audit_logs if l.has_anomaly()]),
        "alert_count": len(state.alerts),
        "backup_plan_count": len(state.backup_plans),
        "fault_event_count": len(state.fault_events),
        "load_groups": {
            "total_groups": len(config.LOAD_GROUP_CONFIG),
            "active_shed_events": len([e for e in state.load_group_shed_events if e.ended_at is None]),
            "total_shed_events": len(state.load_group_shed_events),
            "all_groups_reported": state.all_groups_reported(),
        },
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
        "POST /api/load/report - 负荷数据上报（整体）",
        "POST /api/load/group/report - 单群组负荷上报",
        "POST /api/load/groups/report - 批量多群组负荷上报",
        "GET /api/load/groups/status - 查询各群组当前状态",
        "PUT /api/load/groups/config - 修改群组配置(额定功率/最大切除比例)",
        "GET /api/load/groups/shed-history - 查询历史切除事件",
        "GET /api/load/groups/reliability - 查询供电可靠性统计",
        "POST /api/dispatch/trigger - 手动触发调度",
        "GET /api/status/sources - 发电源状态",
        "GET /api/status/bess - 电池状态",
        "GET /api/battery/health - 电池健康报告",
        "POST /api/battery/health/reset-baseline - 重置电池内阻基线",
        "GET /api/status/tariff - 电价信息",
        "GET /api/status/load - 负荷状态",
        "GET /api/dispatch/history - 调度历史",
        "GET /api/stats/accumulated - 累计统计",
        "GET /api/alerts - 告警记录",
        "GET /api/source/health - 所有发电源健康评分",
        "GET /api/source/health/<type>/<id> - 单个发电源健康评分",
        "GET /api/source/health/<type>/<id>/history - 发电源健康评分历史趋势",
        "POST /api/source/maintenance - 设置发电源维护状态",
        "GET /api/backup-plans - 备用预案列表",
        "GET /api/backup-plans/<type>/<id> - 单源备用预案",
        "GET /api/fault-events - 故障事件记录",
        "PUT /api/config/tariff - 修改电价配置",
        "PUT /api/config/bess_soc - 修改SOC区间",
        "GET /api/config/all - 查看全部配置",
        "GET /api/audit/logs - 审计日志列表（支持筛选）",
        "GET /api/audit/logs/<audit_id> - 单条审计日志详情",
        "GET /api/audit/anomalies - 异常决策列表",
        "GET /api/audit/compare - 决策对比接口",
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
    print("=" * 60)
    print("服务地址: http://127.0.0.1:5001")
    print("健康检查: http://127.0.0.1:5001/api/health")
    print("审计日志: http://127.0.0.1:5001/api/audit/logs")
    print("异常检测: http://127.0.0.1:5001/api/audit/anomalies")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5001, debug=False)
