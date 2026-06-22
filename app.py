from datetime import datetime, timedelta
from typing import Optional
from flask import Flask, request, jsonify
from dataclasses import asdict

import config
from models import MicrogridState, SourceReport, LoadReport
from dispatcher import DispatchEngine
from audit import DecisionComparator
from decision_replay import DecisionReplayEngine, LOW_SCORE_THRESHOLD

app = Flask(__name__)

state = MicrogridState()
engine = DispatchEngine(state)
replay_engine = DecisionReplayEngine(state)


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


def _cost_attribution_to_dict(attr):
    return {
        "attribution_id": attr.attribution_id,
        "dispatch_id": attr.dispatch_id,
        "timestamp": attr.timestamp.isoformat(),
        "grid_purchase_cost": attr.grid_purchase_cost,
        "diesel_generation_cost": attr.diesel_generation_cost,
        "diesel_startup_cost": attr.diesel_startup_cost,
        "load_shed_penalty_cost": attr.load_shed_penalty_cost,
        "bess_loss_cost": attr.bess_loss_cost,
        "feed_in_revenue": attr.feed_in_revenue,
        "total_comprehensive_cost": attr.total_comprehensive_cost,
        "details": attr.details,
    }


def _missed_opportunity_to_dict(opp):
    return {
        "opportunity_id": opp.opportunity_id,
        "dispatch_id": opp.dispatch_id,
        "timestamp": opp.timestamp.isoformat(),
        "high_soc_savings": opp.high_soc_savings,
        "valley_hour_savings": opp.valley_hour_savings,
        "total_missed_savings": opp.total_missed_savings,
        "details": opp.details,
    }


@app.route("/api/cost-attribution/by-dispatch/<dispatch_id>", methods=["GET"])
def get_cost_attribution_by_dispatch(dispatch_id):
    """
    查询单次调度的成本归因明细（通过调度ID关联）
    """
    attribution = state.get_cost_attribution_by_dispatch_id(dispatch_id)
    if attribution is None:
        return jsonify({"error": f"未找到调度ID对应的成本归因: {dispatch_id}"}), 404

    opportunity = state.get_missed_opportunity_by_dispatch_id(dispatch_id)

    return jsonify({
        "status": "ok",
        "cost_attribution": _cost_attribution_to_dict(attribution),
        "missed_opportunity": _missed_opportunity_to_dict(opportunity) if opportunity else None,
    })


@app.route("/api/cost-attribution/summary", methods=["GET"])
def get_cost_attribution_summary():
    """
    按时间段聚合的成本归因报告
    参数:
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
    """
    try:
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

        summary = state.compute_cost_attribution_summary(start_time, end_time)

        breakdown = {}
        total_cost = max(0.0001, summary.total_comprehensive_cost)
        for key, value in summary.breakdown.items():
            breakdown[key] = {
                "amount": round(value, 4),
                "ratio": round(value / total_cost * 100, 2) if total_cost > 0 else 0.0,
            }

        return jsonify({
            "status": "ok",
            "summary": {
                "total_grid_purchase_cost": summary.total_grid_purchase_cost,
                "total_diesel_cost": summary.total_diesel_cost,
                "total_load_shed_penalty": summary.total_load_shed_penalty,
                "total_bess_loss_cost": summary.total_bess_loss_cost,
                "total_feed_in_revenue": summary.total_feed_in_revenue,
                "total_comprehensive_cost": summary.total_comprehensive_cost,
                "total_missed_savings": summary.total_missed_savings,
                "dispatch_count": summary.dispatch_count,
                "breakdown_with_ratio": breakdown,
            },
            "time_range": {
                "start_time": start_time_str,
                "end_time": end_time_str,
            },
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/cost-attribution/trend", methods=["GET"])
def get_cost_trend():
    """
    成本趋势分析（按小时/按天聚合）
    参数:
      - granularity: 聚合粒度，可选 hour/day，默认 hour
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
    """
    try:
        granularity = request.args.get("granularity", "hour")
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")

        if granularity not in ("hour", "day"):
            return jsonify({"error": "granularity 必须是 hour 或 day"}), 400

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

        trend = state.get_cost_trend(granularity, start_time, end_time)

        return jsonify({
            "status": "ok",
            "granularity": granularity,
            "data_points": len(trend),
            "trend": trend,
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/cost-attribution/top-expensive", methods=["GET"])
def get_top_expensive_dispatches():
    """
    查询最贵的N次调度（按综合成本排序返回top N）
    参数:
      - n: 返回数量，默认10，最大100
    """
    try:
        n = int(request.args.get("n", 10))
        n = min(max(1, n), 100)
    except ValueError:
        return jsonify({"error": "n 必须是整数"}), 400

    top_dispatches = state.get_top_n_expensive_dispatches(n)

    result = []
    for attr in top_dispatches:
        opp = state.get_missed_opportunity_by_dispatch_id(attr.dispatch_id)
        result.append({
            "cost_attribution": _cost_attribution_to_dict(attr),
            "missed_opportunity": _missed_opportunity_to_dict(opp) if opp else None,
        })

    return jsonify({
        "status": "ok",
        "requested_count": n,
        "returned_count": len(result),
        "top_dispatches": result,
    })


@app.route("/api/cost-attribution/config/shed-penalty", methods=["GET"])
def get_shed_penalty_config():
    """
    查询甩负荷惩罚系数配置
    """
    return jsonify({
        "status": "ok",
        "config": {
            "load_shed_penalty_per_kwh": config.COST_ATTRIBUTION_CONFIG["load_shed_penalty_per_kwh"],
        },
    })


@app.route("/api/cost-attribution/config/shed-penalty", methods=["PUT"])
def update_shed_penalty_config():
    """
    修改甩负荷惩罚系数，修改后只影响新产生的归因记录，不追溯历史
    请求体: {
        "load_shed_penalty_per_kwh": 3.0
    }
    """
    data = request.get_json(force=True) or {}

    if "load_shed_penalty_per_kwh" not in data:
        return jsonify({"error": "缺少必填字段: load_shed_penalty_per_kwh"}), 400

    new_penalty = float(data["load_shed_penalty_per_kwh"])
    if new_penalty < 0:
        return jsonify({"error": "甩负荷惩罚系数不能为负数"}), 400

    config.COST_ATTRIBUTION_CONFIG["load_shed_penalty_per_kwh"] = new_penalty

    return jsonify({
        "status": "ok",
        "message": "甩负荷惩罚系数已更新，立即生效（仅影响新的归因记录）",
        "config": {
            "load_shed_penalty_per_kwh": new_penalty,
        },
    })


@app.route("/api/cost-attribution/missed-opportunities/summary", methods=["GET"])
def get_missed_opportunities_summary():
    """
    错过的套利机会统计（"本可节省"分析）
    参数:
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
    """
    try:
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

        summary = state.compute_cost_attribution_summary(start_time, end_time)

        filtered_opps = []
        for opp in state.missed_opportunities:
            if start_time and opp.timestamp < start_time:
                continue
            if end_time and opp.timestamp > end_time:
                continue
            filtered_opps.append(opp)

        total_high_soc = sum(o.high_soc_savings for o in filtered_opps)
        total_valley_hour = sum(o.valley_hour_savings for o in filtered_opps)

        arbitrage_stats = state.get_arbitrage_stats_report()
        actual_savings = arbitrage_stats.get("net_profit", 0.0)
        total_potential = actual_savings + summary.total_missed_savings

        return jsonify({
            "status": "ok",
            "summary": {
                "actual_arbitrage_savings": round(actual_savings, 4),
                "missed_high_soc_savings": round(total_high_soc, 4),
                "missed_valley_hour_savings": round(total_valley_hour, 4),
                "total_missed_savings": round(summary.total_missed_savings, 4),
                "total_potential_savings": round(total_potential, 4),
                "capture_ratio": round(
                    (actual_savings / total_potential * 100) if total_potential > 0 else 0.0,
                    2
                ),
                "opportunity_count": len(filtered_opps),
            },
            "time_range": {
                "start_time": start_time_str,
                "end_time": end_time_str,
            },
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


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


@app.route("/api/dynamic-shed/pressure", methods=["GET"])
def get_power_pressure():
    """
    查询当前供电压力指数和模式
    返回: 当前压力指数、模式、中文模式名、是否手动锁定
    """
    info = state.get_power_pressure_info()
    restore_status = state.get_emergency_restore_status()
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "pressure": info,
        "emergency_restore": restore_status,
    })


@app.route("/api/dynamic-shed/pressure/history", methods=["GET"])
def get_power_pressure_history_api():
    """
    查询压力指数历史趋势（最近50个点）
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    history = state.get_power_pressure_history(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total_points": len(state.power_pressure_history),
        "returned": len(history),
        "history": history,
    })


@app.route("/api/dynamic-shed/limits", methods=["GET"])
def get_dynamic_shed_limits_api():
    """
    查询各群组当前动态限额（实际生效的切除比例vs配置的切除比例）
    """
    limits = state.get_dynamic_shed_limits()
    pressure_info = state.get_power_pressure_info()
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "current_mode": pressure_info["current_mode"],
        "current_mode_chinese": pressure_info["current_mode_chinese"],
        "pressure_index": pressure_info["current_pressure_index"],
        "groups": limits,
    })


@app.route("/api/dynamic-shed/mode-history", methods=["GET"])
def get_shed_mode_history_api():
    """
    查询模式切换历史记录（什么时候从哪个模式切到哪个模式、触发原因）
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    history = state.get_shed_mode_history(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total_switches": len(state.shed_mode_history),
        "returned": len(history),
        "current_mode": state.current_shed_mode,
        "manual_lock": state.shed_mode_manual_lock,
        "history": history,
    })


@app.route("/api/dynamic-shed/mode-lock", methods=["POST"])
def set_shed_mode_lock():
    """
    手动锁定/解锁模式
    请求体: {
        "lock": true/false,        (true=锁定, false=解锁)
        "mode": "emergency"         (lock=true时必填，可选值: relaxed/normal/emergency)
    }
    锁定后不再自动切换，直到手动解锁
    """
    data = request.get_json(force=True)
    if "lock" not in data:
        return jsonify({"error": "缺少必填字段: lock"}), 400

    lock = bool(data["lock"])
    mode = data.get("mode")

    valid_modes = {"relaxed", "normal", "emergency"}
    if lock:
        if mode not in valid_modes:
            return jsonify({"error": f"lock=true时mode必须是 {valid_modes} 之一"}), 400

    success = state.set_shed_mode_manual_lock(lock, mode)
    if not success:
        return jsonify({"error": "模式设置失败，无效的mode值"}), 400

    pressure_info = state.get_power_pressure_info()
    mode_history = state.get_shed_mode_history(limit=1)

    return jsonify({
        "status": "ok",
        "message": "模式已锁定" if lock else "模式已解锁，恢复自动切换",
        "lock": lock,
        "manual_mode": mode if lock else None,
        "current_mode": pressure_info["current_mode"],
        "current_mode_chinese": pressure_info["current_mode_chinese"],
        "pressure_index": pressure_info["current_pressure_index"],
        "latest_switch": mode_history[0] if mode_history else None,
    })


@app.route("/api/dynamic-shed/config", methods=["GET"])
def get_dynamic_shed_config():
    """查询动态限额功能的当前配置参数"""
    cfg = config.DYNAMIC_SHED_CONFIG
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "config": cfg,
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


def _daily_report_to_dict(report):
    return {
        "report_id": report.report_id,
        "report_type": report.report_type,
        "report_date": report.report_date,
        "generated_at": report.generated_at.isoformat(),
        "dispatch_count": report.dispatch_count,
        "grid_purchase_cost": round(report.grid_purchase_cost, 4),
        "diesel_total_cost": round(report.diesel_total_cost, 4),
        "diesel_generation_cost": round(report.diesel_generation_cost, 4),
        "diesel_startup_cost": round(report.diesel_startup_cost, 4),
        "load_shed_penalty": round(report.load_shed_penalty, 4),
        "feed_in_revenue": round(report.feed_in_revenue, 4),
        "net_cost": round(report.net_cost, 4),
        "prev_day_net_cost": round(report.prev_day_net_cost, 4) if report.prev_day_net_cost is not None else None,
        "net_cost_change_percent": report.net_cost_change_percent,
        "net_cost_change_direction": "上升" if report.net_cost_change_percent and report.net_cost_change_percent > 0 else "下降" if report.net_cost_change_percent and report.net_cost_change_percent < 0 else "持平",
        "top_expensive_dispatches": [
            {
                "dispatch_id": d.dispatch_id,
                "timestamp": d.timestamp.isoformat(),
                "total_cost": round(d.total_cost, 4),
                "cost_breakdown": d.cost_breakdown,
                "reason": d.reason,
            }
            for d in report.top_expensive_dispatches
        ],
        "battery_stats": {
            bes_id: {
                "total_charged_kwh": round(bs.total_charged_kwh, 4),
                "total_discharged_kwh": round(bs.total_discharged_kwh, 4),
                "soc_min_percent": round(bs.soc_min * 100, 2),
                "soc_max_percent": round(bs.soc_max * 100, 2),
                "cycle_increment": round(bs.cycle_increment, 4),
                "start_soc_percent": round(bs.start_soc * 100, 2),
                "end_soc_percent": round(bs.end_soc * 100, 2),
            }
            for bes_id, bs in report.battery_stats.items()
        },
        "load_group_reliability": [
            {
                "group_id": r.group_id,
                "group_name": r.group_name,
                "reliability_percent": round(r.reliability_percent, 2),
                "total_snapshots": r.total_snapshots,
                "shed_snapshots": r.shed_snapshots,
            }
            for r in report.load_group_reliability
        ],
        "valley_purchase_ratio": round(report.valley_purchase_ratio, 4),
        "valley_purchase_ratio_percent": round(report.valley_purchase_ratio * 100, 2),
        "total_grid_import_kwh": round(report.total_grid_import_kwh, 4),
        "valley_grid_import_kwh": round(report.valley_grid_import_kwh, 4),
        "total_load_shed_events": report.total_load_shed_events,
        "total_load_shed_duration_minutes": round(report.total_load_shed_duration_minutes, 2),
        "renewable_surplus_kwh": round(report.renewable_surplus_kwh, 4),
        "suggestions": [
            {
                "type": s.type,
                "severity": s.severity,
                "title": s.title,
                "description": s.description,
                "data": s.data,
            }
            for s in report.suggestions
        ],
        "suggestion_count": len(report.suggestions),
    }


def _weekly_report_to_dict(report):
    return {
        "report_id": report.report_id,
        "report_type": report.report_type,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "generated_at": report.generated_at.isoformat(),
        "total_dispatch_count": report.total_dispatch_count,
        "avg_daily_dispatch_count": round(report.avg_daily_dispatch_count, 2),
        "total_grid_purchase_cost": round(report.total_grid_purchase_cost, 4),
        "avg_daily_grid_purchase_cost": round(report.avg_daily_grid_purchase_cost, 4),
        "total_diesel_cost": round(report.total_diesel_cost, 4),
        "total_load_shed_penalty": round(report.total_load_shed_penalty, 4),
        "total_feed_in_revenue": round(report.total_feed_in_revenue, 4),
        "total_net_cost": round(report.total_net_cost, 4),
        "avg_daily_net_cost": round(report.avg_daily_net_cost, 4),
        "most_expensive_day": report.most_expensive_day,
        "most_expensive_day_cost": round(report.most_expensive_day_cost, 4),
        "cheapest_day": report.cheapest_day,
        "cheapest_day_cost": round(report.cheapest_day_cost, 4),
        "daily_trend": report.daily_trend,
        "storage_arbitrage_profit": round(report.storage_arbitrage_profit, 4),
        "total_load_shed_events": report.total_load_shed_events,
        "total_load_shed_duration_minutes": round(report.total_load_shed_duration_minutes, 2),
        "suggestions": [
            {
                "type": s.type,
                "severity": s.severity,
                "title": s.title,
                "description": s.description,
                "data": s.data,
            }
            for s in report.suggestions
        ],
        "suggestion_count": len(report.suggestions),
    }


def _report_brief_to_dict(report):
    if report.report_type == "daily":
        return {
            "report_id": report.report_id,
            "report_type": report.report_type,
            "report_date": report.report_date,
            "generated_at": report.generated_at.isoformat(),
            "dispatch_count": report.dispatch_count,
            "net_cost": round(report.net_cost, 4),
            "suggestion_count": len(report.suggestions),
        }
    else:
        return {
            "report_id": report.report_id,
            "report_type": report.report_type,
            "start_date": report.start_date,
            "end_date": report.end_date,
            "generated_at": report.generated_at.isoformat(),
            "total_dispatch_count": report.total_dispatch_count,
            "total_net_cost": round(report.total_net_cost, 4),
            "suggestion_count": len(report.suggestions),
        }


@app.route("/api/report/daily", methods=["POST"])
def generate_daily_report():
    """
    生成日报
    请求体: {
        "date": "2024-06-18"    (可选，默认当天)
    }
    同一天重复生成会覆盖旧的报告
    """
    try:
        data = request.get_json(force=True) or {}
        date_str = data.get("date")
        
        report = state.generate_daily_report(date_str)
        
        return jsonify({
            "status": "ok",
            "message": "日报生成成功",
            "report": _daily_report_to_dict(report),
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/report/weekly", methods=["POST"])
def generate_weekly_report():
    """
    生成周报
    请求体: {
        "start_date": "2024-06-10",
        "end_date": "2024-06-16"
    }
    相同起止日期重复生成会覆盖旧的报告
    """
    try:
        data = request.get_json(force=True) or {}
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        
        if not start_date or not end_date:
            return jsonify({"error": "缺少必填字段: start_date 和 end_date"}), 400
        
        report = state.generate_weekly_report(start_date, end_date)
        
        return jsonify({
            "status": "ok",
            "message": "周报生成成功",
            "report": _weekly_report_to_dict(report),
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/report/list", methods=["GET"])
def list_reports():
    """
    列出所有报告
    参数:
      - type: 可选，daily/weekly，按类型筛选
    """
    report_type = request.args.get("type")
    if report_type and report_type not in ("daily", "weekly"):
        return jsonify({"error": "type 必须是 daily 或 weekly"}), 400
    
    reports = state.list_reports(report_type)
    
    return jsonify({
        "status": "ok",
        "total": len(reports),
        "type_filter": report_type,
        "reports": [_report_brief_to_dict(r) for r in reports],
    })


@app.route("/api/report/<report_id>", methods=["GET"])
def get_report_detail(report_id):
    """
    查看单份报告详情
    """
    report = state.get_report_by_id(report_id)
    if report is None:
        return jsonify({"error": f"未找到报告: {report_id}"}), 404
    
    if report.report_type == "daily":
        return jsonify({
            "status": "ok",
            "report": _daily_report_to_dict(report),
        })
    else:
        return jsonify({
            "status": "ok",
            "report": _weekly_report_to_dict(report),
        })


@app.route("/api/report/<report_id>", methods=["DELETE"])
def delete_report(report_id):
    """
    删除报告
    """
    success = state.delete_report(report_id)
    if not success:
        return jsonify({"error": f"未找到报告: {report_id}"}), 404
    
    return jsonify({
        "status": "ok",
        "message": "报告已删除",
        "report_id": report_id,
    })


@app.route("/api/alerts/active", methods=["GET"])
def get_active_alerts():
    """查询当前活跃告警列表（未确认），按级别筛选，按时间排序"""
    level = request.args.get("level")
    if level and level not in ("INFO", "WARNING", "CRITICAL"):
        return jsonify({"error": "level 必须是 INFO, WARNING, CRITICAL 之一"}), 400
    alerts = state.alert_manager.get_active_alerts(level=level)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(alerts),
        "level_filter": level or "全部",
        "alerts": [a.to_dict() for a in alerts],
    })


@app.route("/api/alerts/<alert_id>", methods=["GET"])
def get_alert_detail(alert_id):
    """查询单条告警详情"""
    alert = state.alert_manager.get_alert(alert_id)
    if alert is None:
        return jsonify({"error": f"未找到告警: {alert_id}"}), 404
    return jsonify({
        "status": "ok",
        "alert": alert.to_dict(),
    })


@app.route("/api/alerts/<alert_id>/escalation-history", methods=["GET"])
def get_alert_escalation_history(alert_id):
    """查询告警升级历史时间线"""
    history = state.alert_manager.get_alert_escalation_history(alert_id)
    if history is None:
        return jsonify({"error": f"未找到告警: {alert_id}"}), 404
    return jsonify({
        "status": "ok",
        "alert_id": alert_id,
        "total_events": len(history),
        "timeline": history,
    })


@app.route("/api/alerts/<alert_id>/acknowledge", methods=["POST"])
def acknowledge_alert(alert_id):
    """确认单条告警，确认后停止升级流程"""
    data = request.get_json(silent=True) or {}
    acknowledged_by = data.get("acknowledged_by")
    success = state.alert_manager.acknowledge_alert(alert_id, acknowledged_by)
    if not success:
        return jsonify({"error": f"未找到告警或已确认: {alert_id}"}), 404
    alert = state.alert_manager.get_alert(alert_id)
    return jsonify({
        "status": "ok",
        "message": f"告警 {alert_id} 已确认",
        "alert": alert.to_dict() if alert else None,
    })


@app.route("/api/alerts/acknowledge-by-type", methods=["POST"])
def acknowledge_alerts_by_type():
    """批量确认同类型的所有未确认告警"""
    data = request.get_json(silent=True) or {}
    alert_type = data.get("alert_type")
    acknowledged_by = data.get("acknowledged_by")
    if not alert_type:
        return jsonify({"error": "缺少必填字段: alert_type"}), 400
    count = state.alert_manager.acknowledge_alerts_by_type(alert_type, acknowledged_by)
    return jsonify({
        "status": "ok",
        "message": f"已确认 {count} 条 {alert_type} 类型告警",
        "acknowledged_count": count,
        "alert_type": alert_type,
    })


@app.route("/api/alerts/check-escalation", methods=["POST"])
def check_alerts_escalation():
    """手动触发基于时间的告警升级检查（WARNING -> CRITICAL）"""
    state.alert_manager.process_pending_notifications()
    escalated = state.alert_manager.check_time_based_escalation()
    return jsonify({
        "status": "ok",
        "message": f"已检查升级，{len(escalated)} 条告警升级为紧急级别",
        "escalated_count": len(escalated),
        "escalated_alerts": [a.to_dict() for a in escalated],
    })


@app.route("/api/alerts/statistics", methods=["GET"])
def get_alert_statistics():
    """告警统计：按类型和级别聚合的计数，最近24小时的告警趋势"""
    stats = state.alert_manager.get_alert_statistics()
    return jsonify({
        "status": "ok",
        "statistics": stats,
    })


@app.route("/api/duty/staff", methods=["GET"])
def list_duty_staff():
    """查询所有值班人员配置"""
    staff_list = state.alert_manager.list_duty_staff()
    now = datetime.now()
    current_on_duty = [s.staff_id for s in state.alert_manager.get_on_duty_staff(now)]
    result = []
    for s in staff_list:
        d = s.to_dict()
        d["is_on_duty_now"] = s.staff_id in current_on_duty
        result.append(d)
    return jsonify({
        "status": "ok",
        "query_time": now.isoformat(),
        "total": len(result),
        "staff": result,
    })


@app.route("/api/duty/staff", methods=["POST"])
def add_duty_staff():
    """新增值班人员配置"""
    data = request.get_json(silent=True) or {}
    required = ["name", "contact", "start_hour", "end_hour"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400
    try:
        start_hour = int(data["start_hour"])
        end_hour = int(data["end_hour"])
    except (ValueError, TypeError):
        return jsonify({"error": "start_hour 和 end_hour 必须是整数"}), 400
    try:
        staff = state.alert_manager.add_duty_staff(
            name=data["name"],
            contact=data["contact"],
            start_hour=start_hour,
            end_hour=end_hour,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "status": "ok",
        "message": "值班人员添加成功",
        "staff": staff.to_dict(),
    })


@app.route("/api/duty/staff/<staff_id>", methods=["GET"])
def get_duty_staff_detail(staff_id):
    """查询单个值班人员配置"""
    staff = state.alert_manager.get_duty_staff(staff_id)
    if staff is None:
        return jsonify({"error": f"未找到值班人员: {staff_id}"}), 404
    now = datetime.now()
    d = staff.to_dict()
    d["is_on_duty_now"] = state.alert_manager.is_on_duty(staff, now)
    return jsonify({
        "status": "ok",
        "query_time": now.isoformat(),
        "staff": d,
    })


@app.route("/api/duty/staff/<staff_id>", methods=["PUT"])
def update_duty_staff(staff_id):
    """修改值班人员配置"""
    data = request.get_json(silent=True) or {}
    start_hour = data.get("start_hour")
    end_hour = data.get("end_hour")
    if start_hour is not None:
        try:
            start_hour = int(start_hour)
        except (ValueError, TypeError):
            return jsonify({"error": "start_hour 必须是整数"}), 400
    if end_hour is not None:
        try:
            end_hour = int(end_hour)
        except (ValueError, TypeError):
            return jsonify({"error": "end_hour 必须是整数"}), 400
    try:
        success = state.alert_manager.update_duty_staff(
            staff_id=staff_id,
            name=data.get("name"),
            contact=data.get("contact"),
            start_hour=start_hour,
            end_hour=end_hour,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not success:
        return jsonify({"error": f"未找到值班人员: {staff_id}"}), 404
    staff = state.alert_manager.get_duty_staff(staff_id)
    return jsonify({
        "status": "ok",
        "message": "值班人员配置已更新",
        "staff": staff.to_dict() if staff else None,
    })


@app.route("/api/duty/staff/<staff_id>", methods=["DELETE"])
def delete_duty_staff(staff_id):
    """删除值班人员配置"""
    success = state.alert_manager.delete_duty_staff(staff_id)
    if not success:
        return jsonify({"error": f"未找到值班人员: {staff_id}"}), 404
    return jsonify({
        "status": "ok",
        "message": "值班人员已删除",
        "staff_id": staff_id,
    })


@app.route("/api/duty/on-duty-now", methods=["GET"])
def get_on_duty_now():
    """查询当前谁在值班"""
    now = datetime.now()
    on_duty = state.alert_manager.get_on_duty_staff(now)
    next_time = state.alert_manager.get_next_on_duty_time(now)
    return jsonify({
        "status": "ok",
        "query_time": now.isoformat(),
        "current_hour": now.hour,
        "on_duty_count": len(on_duty),
        "on_duty": [s.to_dict() for s in on_duty],
        "next_on_duty_time": next_time.isoformat() if next_time else None,
        "has_on_duty": len(on_duty) > 0,
    })


@app.route("/api/duty/notifications", methods=["GET"])
def get_notifications():
    """查询通知队列（已发送/待发送/无人接收）"""
    status = request.args.get("status")
    valid_statuses = ("pending", "sent", "unattended", "cancelled")
    if status and status not in valid_statuses:
        return jsonify({"error": f"status 必须是 {valid_statuses} 之一"}), 400
    state.alert_manager.process_pending_notifications()
    notifications = state.alert_manager.get_notifications(status=status)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "status_filter": status or "全部",
        "total": len(notifications),
        "notifications": [n.to_dict() for n in notifications],
    })


@app.route("/api/duty/notifications/process", methods=["POST"])
def process_notifications():
    """手动处理待发送/无人接收的通知"""
    now = datetime.now()
    state.alert_manager.process_pending_notifications(now)
    all_notifications = state.alert_manager.get_notifications()
    stats = {
        "total": len(all_notifications),
        "sent": len([n for n in all_notifications if n.status == "sent"]),
        "pending": len([n for n in all_notifications if n.status == "pending"]),
        "unattended": len([n for n in all_notifications if n.status == "unattended"]),
    }
    return jsonify({
        "status": "ok",
        "message": "通知队列已处理",
        "statistics": stats,
    })


@app.route("/api/carbon/quota/status", methods=["GET"])
def get_carbon_quota_status():
    """查询当月碳排放累计值和配额使用率"""
    if not config.CARBON_CONFIG.get("enable_carbon_tracking", False):
        return jsonify({"error": "碳排放追踪未启用"}), 400
    status = state.carbon_manager.get_current_quota_state()
    return jsonify({
        "status": "ok",
        "data": status,
    })


@app.route("/api/carbon/records", methods=["GET"])
def get_carbon_emission_records():
    """查询碳排放明细（每次调度贡献了多少，按柴油和购电分开）
    参数:
      - start_time: ISO格式时间字符串，可选
      - end_time: ISO格式时间字符串，可选
      - limit: 返回条数，默认100
      - offset: 偏移量，默认0
    """
    if not config.CARBON_CONFIG.get("enable_carbon_tracking", False):
        return jsonify({"error": "碳排放追踪未启用"}), 400

    start_time_str = request.args.get("start_time")
    end_time_str = request.args.get("end_time")
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))

    start_time = None
    end_time = None
    try:
        if start_time_str:
            start_time = datetime.fromisoformat(start_time_str)
        if end_time_str:
            end_time = datetime.fromisoformat(end_time_str)
    except ValueError:
        return jsonify({"error": "时间格式错误，请使用ISO格式"}), 400

    records = state.carbon_manager.get_emission_records(
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )

    result = []
    for r in records:
        result.append({
            "record_id": r.record_id,
            "dispatch_id": r.dispatch_id,
            "timestamp": r.timestamp.isoformat(),
            "diesel_emission_kg": r.diesel_emission_kg,
            "grid_emission_kg": r.grid_emission_kg,
            "total_emission_kg": r.total_emission_kg,
            "diesel_generated_kwh": r.diesel_generated_kwh,
            "grid_import_kwh": r.grid_import_kwh,
            "carbon_status": r.carbon_status,
            "quota_remaining_ratio": r.quota_remaining_ratio,
        })

    return jsonify({
        "status": "ok",
        "total": len(state.carbon_manager.carbon_emission_records),
        "count": len(result),
        "limit": limit,
        "offset": offset,
        "records": result,
    })


@app.route("/api/carbon/quota", methods=["PUT"])
def update_carbon_quota():
    """修改月度碳配额值（立即生效影响当月剩余判断）
    请求体: {
        "monthly_quota_kg": 6000.0
    }
    """
    if not config.CARBON_CONFIG.get("enable_carbon_tracking", False):
        return jsonify({"error": "碳排放追踪未启用"}), 400

    data = request.get_json(silent=True) or {}
    new_quota = data.get("monthly_quota_kg")
    if new_quota is None or new_quota <= 0:
        return jsonify({"error": "请提供有效的月度配额值（monthly_quota_kg > 0）"}), 400

    success = state.carbon_manager.update_monthly_quota(float(new_quota))
    if not success:
        return jsonify({"error": "配额修改失败"}), 500

    status = state.carbon_manager.get_current_quota_state()
    return jsonify({
        "status": "ok",
        "message": "月度碳配额已更新",
        "data": status,
    })


@app.route("/api/carbon/daily-trend", methods=["GET"])
def get_carbon_daily_trend():
    """查询碳排放日趋势（每天排了多少）
    参数:
      - start_date: 开始日期，格式 YYYY-MM-DD，可选
      - end_date: 结束日期，格式 YYYY-MM-DD，可选
    """
    if not config.CARBON_CONFIG.get("enable_carbon_tracking", False):
        return jsonify({"error": "碳排放追踪未启用"}), 400

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    trend = state.carbon_manager.get_daily_trend(
        start_date=start_date,
        end_date=end_date,
    )

    return jsonify({
        "status": "ok",
        "days": len(trend),
        "trend": trend,
    })


@app.route("/api/carbon/reset", methods=["POST"])
def reset_carbon_accumulated():
    """手动重置当月累计（用于配额购买后增加额度的场景）
    请求体: {
        "add_quota_kg": 1000.0    (可选，增加的配额量)
    }
    """
    if not config.CARBON_CONFIG.get("enable_carbon_tracking", False):
        return jsonify({"error": "碳排放追踪未启用"}), 400

    data = request.get_json(silent=True) or {}
    add_quota_kg = float(data.get("add_quota_kg", 0.0))

    success = state.carbon_manager.reset_monthly_accumulated(add_quota_kg=add_quota_kg)
    if not success:
        return jsonify({"error": "重置失败"}), 500

    status = state.carbon_manager.get_current_quota_state()
    return jsonify({
        "status": "ok",
        "message": "当月累计碳排放已重置" if add_quota_kg <= 0 else f"当月累计已重置，配额增加 {add_quota_kg}kg",
        "add_quota_kg": add_quota_kg,
        "data": status,
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
        "daily_report_count": len(state.daily_reports),
        "weekly_report_count": len(state.weekly_reports),
        "alert_manager": {
            "active_alerts": len(state.alert_manager.get_active_alerts()),
            "duty_staff_count": len(state.alert_manager.list_duty_staff()),
            "notification_count": len(state.alert_manager.get_notifications()),
        },
        "maintenance": {
            "total_plans": len(state.maintenance_plans),
            "pending_plans": len([p for p in state.maintenance_plans.values() if p.status == "pending"]),
            "active_plans": len([p for p in state.maintenance_plans.values() if p.status == "active"]),
            "completed_plans": len([p for p in state.maintenance_plans.values() if p.status == "completed"]),
            "cancelled_plans": len([p for p in state.maintenance_plans.values() if p.status == "cancelled"]),
            "active_restrictions": len(state._active_maintenance_restrictions),
        },
    })


@app.route("/api/dual-strategy/params", methods=["GET"])
def get_dual_strategy_params():
    """查询当前主策略和影子策略的参数配置"""
    ds = state.dual_strategy_manager
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "enabled": ds.enable,
        "evaluation_cycle_rounds": ds.evaluation_cycle_rounds,
        "cost_improvement_threshold": ds.cost_improvement_threshold,
        "main_strategy": ds.main_strategy.to_dict(),
        "shadow_strategy": ds.shadow_strategy.to_dict(),
    })


@app.route("/api/dual-strategy/shadow-params", methods=["PUT"])
def update_shadow_strategy_params():
    """修改影子策略参数(立即生效影响下一轮对比计算)
    请求体: {
        "battery_discharge_aggressiveness": 0.6,   (可选, 0~1)
        "purchase_tolerance_price": 1.0,           (可选, 元/kWh, null表示用分时电价)
        "shed_trigger_threshold_ratio": 0.05       (可选, 0~1)
    }
    """
    data = request.get_json(force=True) or {}
    result = state.dual_strategy_manager.update_shadow_strategy(
        battery_discharge_aggressiveness=data.get("battery_discharge_aggressiveness"),
        purchase_tolerance_price=data.get("purchase_tolerance_price"),
        shed_trigger_threshold_ratio=data.get("shed_trigger_threshold_ratio"),
    )
    if not result.get("success"):
        return jsonify({"error": result.get("error", "参数更新失败")}), 400
    return jsonify({
        "status": "ok",
        "message": "影子策略参数已更新，立即生效（影响下一轮调度对比计算）",
        "updated": result.get("updated"),
        "current_shadow": result.get("current_shadow"),
    })


@app.route("/api/dual-strategy/progress", methods=["GET"])
def get_dual_strategy_progress():
    """查询当前对比进度(已跑多少轮、两套策略各自的累计成本和甩负荷统计)"""
    ds = state.dual_strategy_manager
    progress = ds.get_progress()
    round_records = []
    for rec in ds.round_records:
        round_records.append({
            "round_index": rec.round_index,
            "timestamp": rec.timestamp.isoformat(),
            "main_cost": round(rec.main_cost, 4),
            "main_shed_kw": round(rec.main_shed_kw, 2),
            "shadow_cost": round(rec.shadow_cost, 4),
            "shadow_shed_kw": round(rec.shadow_shed_kw, 2),
            "main_battery_discharge_kw": round(rec.main_battery_discharge_kw, 2),
            "shadow_battery_discharge_kw": round(rec.shadow_battery_discharge_kw, 2),
        })
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "progress": progress,
        "recent_rounds": round_records[-20:],
        "total_rounds_recorded": len(round_records),
    })


@app.route("/api/dual-strategy/switch-history", methods=["GET"])
def get_dual_strategy_switch_history():
    """查询历史切换记录(什么时候从哪套切到哪套)
    参数: limit (可选, 默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400
    ds = state.dual_strategy_manager
    history = ds.get_switch_history(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total_switches": len(ds.switch_history),
        "returned": len(history),
        "history": history,
    })


@app.route("/api/dual-strategy/evaluate", methods=["POST"])
def dual_strategy_evaluate_now():
    """手动触发立即评估(不等50轮满就做一次对比判断)"""
    ds = state.dual_strategy_manager
    if not ds.enable:
        return jsonify({"error": "双策略功能未启用"}), 400
    now = datetime.now()
    result = ds.evaluate_and_maybe_switch(now=now, trigger="manual_eval")
    return jsonify({
        "status": "ok",
        "query_time": now.isoformat(),
        "evaluation_result": result,
    })


@app.route("/api/dual-strategy/force-switch", methods=["POST"])
def dual_strategy_force_switch():
    """手动强制切换(不管对比结果直接把影子提升为主)"""
    ds = state.dual_strategy_manager
    if not ds.enable:
        return jsonify({"error": "双策略功能未启用"}), 400
    now = datetime.now()
    result = ds.force_switch(now=now)
    return jsonify({
        "status": "ok",
        "query_time": now.isoformat(),
        "switch_result": result,
    })


def _theoretical_optimal_to_dict(opt):
    return {
        "bess_discharge_kw": opt.bess_discharge_kw,
        "grid_import_kw": opt.grid_import_kw,
        "load_served_kw": opt.load_served_kw,
        "load_shed_kw": opt.load_shed_kw,
        "total_cost": opt.total_cost,
        "diesel_output_kw": opt.diesel_output_kw,
        "grid_export_kw": opt.grid_export_kw,
        "cost_breakdown": opt.cost_breakdown,
        "constraints_applied": [
            {
                "constraint_type": c.constraint_type,
                "description": c.description,
                "details": c.details,
            }
            for c in opt.constraints_applied
        ],
    }


def _quality_result_to_dict(result):
    return {
        "replay_id": result.replay_id,
        "audit_id": result.audit_id,
        "dispatch_id": result.dispatch_id,
        "timestamp": result.timestamp.isoformat(),
        "quality_score": result.quality_score,
        "is_low_score": result.is_low_score,
        "score_level": "优秀" if result.quality_score >= 90 else "良好" if result.quality_score >= 70 else "一般" if result.quality_score >= 60 else "较差",
        "actual_cost": result.actual_cost,
        "theoretical_optimal_cost": result.theoretical_optimal_cost,
        "cost_difference": result.cost_difference,
        "cost_savings_potential": result.cost_savings_potential,
        "theoretical_optimal": _theoretical_optimal_to_dict(result.theoretical_optimal),
        "actual_summary": {
            "load_served_kw": result.actual_summary.load_served_kw,
            "load_shed_kw": result.actual_summary.load_shed_kw,
            "load_coverage_ratio": round(result.actual_summary.load_coverage_ratio * 100, 2),
            "total_cost": result.actual_summary.total_cost,
            "pv_share_kw": result.actual_summary.pv_share_kw,
            "wt_share_kw": result.actual_summary.wt_share_kw,
            "diesel_share_kw": result.actual_summary.diesel_share_kw,
            "bess_discharge_kw": result.actual_summary.bess_discharge_kw,
            "grid_import_kw": result.actual_summary.grid_import_kw,
            "grid_export_kw": result.actual_summary.grid_export_kw,
            "cost_breakdown": result.actual_summary.cost_breakdown,
        },
        "constraints_applied": [
            {
                "constraint_type": c.constraint_type,
                "description": c.description,
                "details": c.details,
            }
            for c in result.constraints_applied
        ],
        "explanation": result.explanation,
    }


def _low_score_record_to_dict(record):
    return {
        "quality_result": _quality_result_to_dict(record.quality_result),
        "actual_vs_optimal": record.actual_vs_optimal,
    }


@app.route("/api/decision-replay/single", methods=["POST"])
def replay_single_decision():
    """
    单条历史决策回放评分
    请求体: {
        "audit_id": "AUDIT-00000001"
    }
    返回质量分、理论最优方案、与实际方案的对比
    """
    data = request.get_json(force=True) or {}
    audit_id = data.get("audit_id")

    if not audit_id:
        return jsonify({"error": "缺少必填字段: audit_id"}), 400

    result = replay_engine.replay_single(audit_id)
    if result is None:
        return jsonify({"error": f"未找到审计日志: {audit_id}"}), 404

    return jsonify({
        "status": "ok",
        "message": "回放评分完成",
        "result": _quality_result_to_dict(result),
    })


@app.route("/api/decision-replay/batch", methods=["POST"])
def replay_batch_decisions():
    """
    批量历史决策回放评分
    请求体: {
        "start_time": "2024-06-18T00:00:00",
        "end_time": "2024-06-18T23:59:59"
    }
    返回所有记录的质量分、平均分、以及连续低分告警
    """
    data = request.get_json(force=True) or {}
    start_time_str = data.get("start_time")
    end_time_str = data.get("end_time")

    if not start_time_str or not end_time_str:
        return jsonify({"error": "缺少必填字段: start_time 和 end_time"}), 400

    try:
        start_time = datetime.fromisoformat(start_time_str)
        end_time = datetime.fromisoformat(end_time_str)
    except ValueError:
        return jsonify({"error": "时间格式错误，请使用ISO格式"}), 400

    if start_time >= end_time:
        return jsonify({"error": "start_time 必须小于 end_time"}), 400

    replay_engine.clear_replay_results()
    batch_result = replay_engine.replay_batch(start_time, end_time)

    individual_results = [
        _quality_result_to_dict(r) for r in batch_result.individual_results
    ]

    return jsonify({
        "status": "ok",
        "message": "批量回放评分完成" if not batch_result.alert_generated else "批量回放评分完成，检测到连续低分告警",
        "summary": {
            "start_time": batch_result.start_time.isoformat(),
            "end_time": batch_result.end_time.isoformat(),
            "total_records": batch_result.total_records,
            "avg_quality_score": batch_result.avg_quality_score,
            "min_quality_score": batch_result.min_quality_score,
            "max_quality_score": batch_result.max_quality_score,
            "low_score_count": batch_result.low_score_count,
            "low_score_ratio": batch_result.low_score_ratio,
            "low_score_ratio_percent": round(batch_result.low_score_ratio * 100, 2),
            "consecutive_low_score_warning": batch_result.consecutive_low_score_warning,
            "consecutive_low_score_count": batch_result.consecutive_low_score_count,
            "alert_generated": batch_result.alert_generated,
            "alert_message": batch_result.alert_message,
        },
        "individual_results": individual_results,
    })


@app.route("/api/decision-replay/trend", methods=["GET"])
def get_quality_score_trend():
    """
    查询质量分趋势（按小时/天聚合）
    参数:
      - granularity: 聚合粒度，可选 hour/day，默认 hour
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
    """
    try:
        granularity = request.args.get("granularity", "hour")
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")

        if granularity not in ("hour", "day"):
            return jsonify({"error": "granularity 必须是 hour 或 day"}), 400

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

        trend = replay_engine.get_quality_trend(
            start_time=start_time,
            end_time=end_time,
            granularity=granularity,
        )

        trend_data = []
        for point in trend:
            trend_data.append({
                "time_key": point.time_key,
                "avg_quality_score": point.avg_quality_score,
                "dispatch_count": point.dispatch_count,
                "min_score": point.min_score,
                "max_score": point.max_score,
                "low_score_count": point.low_score_count,
                "low_score_ratio": round(point.low_score_count / point.dispatch_count, 4) if point.dispatch_count > 0 else 0,
            })

        return jsonify({
            "status": "ok",
            "granularity": granularity,
            "total_points": len(trend_data),
            "trend": trend_data,
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/decision-replay/low-scores", methods=["GET"])
def get_low_score_records():
    """
    查询低分记录列表（含实际方案和理论最优方案的对比）
    参数:
      - threshold: 质量分阈值，默认60
      - start_time: 开始时间 (ISO格式，可选)
      - end_time: 结束时间 (ISO格式，可选)
      - limit: 返回数量限制，默认100
    """
    try:
        threshold = float(request.args.get("threshold", LOW_SCORE_THRESHOLD))
        start_time_str = request.args.get("start_time")
        end_time_str = request.args.get("end_time")
        limit = int(request.args.get("limit", 100))

        if threshold < 0 or threshold > 100:
            return jsonify({"error": "threshold 必须在 [0, 100] 区间内"}), 400

        start_time = datetime.fromisoformat(start_time_str) if start_time_str else None
        end_time = datetime.fromisoformat(end_time_str) if end_time_str else None

        records = replay_engine.get_low_score_records(
            threshold=threshold,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        return jsonify({
            "status": "ok",
            "query": {
                "threshold": threshold,
                "start_time": start_time_str,
                "end_time": end_time_str,
                "limit": limit,
            },
            "total_returned": len(records),
            "records": [_low_score_record_to_dict(r) for r in records],
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/decision-replay/config", methods=["GET"])
def get_replay_config():
    """查询回放评分配置参数"""
    from decision_replay import (
        DISCHARGE_STEP_KW, LOW_SCORE_THRESHOLD,
        CONSECUTIVE_LOW_SCORE_THRESHOLD, CONSECUTIVE_LOW_SCORE_ROUNDS,
    )
    return jsonify({
        "status": "ok",
        "config": {
            "discharge_step_kw": DISCHARGE_STEP_KW,
            "low_score_threshold": LOW_SCORE_THRESHOLD,
            "consecutive_low_score_threshold": CONSECUTIVE_LOW_SCORE_THRESHOLD,
            "consecutive_low_score_rounds": CONSECUTIVE_LOW_SCORE_ROUNDS,
            "description": {
                "discharge_step_kw": "电池放电量穷举步长（kW）",
                "low_score_threshold": "低分判定阈值（分），低于此分数标红",
                "consecutive_low_score_threshold": "连续低分告警阈值（分）",
                "consecutive_low_score_rounds": "连续低分告警轮数，连续超过此轮数低于阈值触发告警",
            }
        },
    })


def _arbitrage_settlement_to_dict(s):
    return {
        "settlement_id": s.settlement_id,
        "settlement_date": s.settlement_date,
        "generated_at": s.generated_at.isoformat(),
        "is_recalculation": s.is_recalculation,
        "valley_active_charge_kwh": round(s.valley_active_charge_kwh, 4),
        "valley_passive_charge_kwh": round(s.valley_passive_charge_kwh, 4),
        "valley_total_charge_kwh": round(s.valley_active_charge_kwh + s.valley_passive_charge_kwh, 4),
        "peak_discharge_kwh": round(s.peak_discharge_kwh, 4),
        "valley_charge_price": s.valley_charge_price,
        "peak_discharge_price": s.peak_discharge_price,
        "price_spread": round(s.peak_discharge_price - s.valley_charge_price, 4),
        "theoretical_revenue": round(s.theoretical_revenue, 4),
        "efficiency_loss_cost": round(s.efficiency_loss_cost, 4),
        "net_revenue": round(s.net_revenue, 4),
        "theoretical_max_net_revenue": round(s.theoretical_max_net_revenue, 4),
        "execution_rate": round(s.execution_rate, 2),
        "execution_rate_level": "优秀" if s.execution_rate >= 80 else "良好" if s.execution_rate >= 50 else "较差",
        "interruption_count": s.interruption_count,
        "total_lost_revenue": round(s.total_lost_revenue, 4),
        "interruptions": [
            {
                "interruption_id": i.interruption_id,
                "timestamp": i.timestamp.isoformat(),
                "hour": i.hour,
                "reason": i.reason,
                "reason_category": i.reason_category,
                "planned_mode": i.planned_mode,
                "planned_mode_chinese": {
                    "active_charge": "主动充电",
                    "priority_discharge": "优先放电",
                    "normal": "常规模式",
                }.get(i.planned_mode, "未知"),
                "lost_charge_kwh": round(i.lost_charge_kwh, 4),
                "lost_discharge_kwh": round(i.lost_discharge_kwh, 4),
                "lost_revenue": round(i.lost_revenue, 4),
                "details": i.details,
            }
            for i in s.interruptions
        ],
        "valley_hours_count": s.valley_hours_count,
        "peak_hours_count": s.peak_hours_count,
        "baseline_savings": round(s.baseline_savings, 4),
        "baseline_savings_diff": round(s.net_revenue - s.baseline_savings, 4),
        "notes": s.notes,
        "battery_config": {
            "charge_efficiency": s.charge_efficiency,
            "discharge_efficiency": s.discharge_efficiency,
            "capacity_kwh": s.battery_capacity_kwh,
            "max_charge_power_kw": s.max_charge_power_kw,
            "max_discharge_power_kw": s.max_discharge_power_kw,
        },
    }


@app.route("/api/arbitrage/settlement/<date_str>", methods=["GET"])
def get_arbitrage_settlement(date_str):
    """
    查询指定日期的套利结算详情
    参数:
      - date_str: 日期，格式 YYYY-MM-DD (URL路径参数)
    返回: 充电量/放电量/理论收益/净收益/执行率/中断记录等完整信息
    """
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式错误，应为 YYYY-MM-DD"}), 400

    settlement = state.arbitrage_analyzer.get_settlement(date_str)
    if settlement is None:
        return jsonify({
            "status": "not_found",
            "message": f"日期 {date_str} 暂无套利结算记录",
            "settlement_date": date_str,
        }), 404

    return jsonify({
        "status": "ok",
        "settlement": _arbitrage_settlement_to_dict(settlement),
    })


@app.route("/api/arbitrage/summary", methods=["GET"])
def get_arbitrage_summary():
    """
    查询日期范围内的套利汇总
    参数:
      - start_date: 开始日期，格式 YYYY-MM-DD
      - end_date: 结束日期，格式 YYYY-MM-DD
    返回: 总净收益/平均执行率/中断次数等汇总统计
    """
    try:
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        if not start_date_str or not end_date_str:
            return jsonify({"error": "缺少必填参数: start_date 和 end_date"}), 400

        from datetime import datetime
        datetime.strptime(start_date_str, "%Y-%m-%d")
        datetime.strptime(end_date_str, "%Y-%m-%d")

        summary = state.arbitrage_analyzer.get_settlement_summary(start_date_str, end_date_str)

        return jsonify({
            "status": "ok",
            "summary": {
                "start_date": summary.start_date,
                "end_date": summary.end_date,
                "settlement_count": summary.settlement_count,
                "total_net_revenue": round(summary.total_net_revenue, 4),
                "total_theoretical_revenue": round(summary.total_theoretical_revenue, 4),
                "total_efficiency_loss_cost": round(summary.total_efficiency_loss_cost, 4),
                "avg_execution_rate": round(summary.avg_execution_rate, 2),
                "total_interruptions": summary.total_interruptions,
                "total_lost_revenue": round(summary.total_lost_revenue, 4),
                "total_valley_active_charge_kwh": round(summary.total_valley_active_charge_kwh, 4),
                "total_valley_passive_charge_kwh": round(summary.total_valley_passive_charge_kwh, 4),
                "total_valley_charge_kwh": round(
                    summary.total_valley_active_charge_kwh + summary.total_valley_passive_charge_kwh, 4
                ),
                "total_peak_discharge_kwh": round(summary.total_peak_discharge_kwh, 4),
                "total_baseline_savings": round(summary.total_baseline_savings, 4),
                "low_execution_rate_days": summary.low_execution_rate_days,
                "high_execution_rate_days": summary.high_execution_rate_days,
                "avg_net_revenue_per_day": round(
                    summary.total_net_revenue / summary.settlement_count, 4
                ) if summary.settlement_count > 0 else 0.0,
            },
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/arbitrage/trend", methods=["GET"])
def get_arbitrage_trend():
    """
    查询套利趋势（按天的净收益和执行率走势）
    参数:
      - start_date: 开始日期，格式 YYYY-MM-DD
      - end_date: 结束日期，格式 YYYY-MM-DD
    返回: 每日净收益、执行率、充放电量等趋势数据
    """
    try:
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        if not start_date_str or not end_date_str:
            return jsonify({"error": "缺少必填参数: start_date 和 end_date"}), 400

        from datetime import datetime
        datetime.strptime(start_date_str, "%Y-%m-%d")
        datetime.strptime(end_date_str, "%Y-%m-%d")

        trend = state.arbitrage_analyzer.get_trend(start_date_str, end_date_str)

        trend_data = []
        for point in trend:
            trend_data.append({
                "date": point.date,
                "net_revenue": round(point.net_revenue, 4),
                "execution_rate": round(point.execution_rate, 2),
                "execution_rate_level": "优秀" if point.execution_rate >= 80 else "良好" if point.execution_rate >= 50 else "较差",
                "theoretical_revenue": round(point.theoretical_revenue, 4),
                "interruption_count": point.interruption_count,
                "valley_charge_kwh": round(point.valley_charge_kwh, 4),
                "peak_discharge_kwh": round(point.peak_discharge_kwh, 4),
            })

        total_net = sum(p.net_revenue for p in trend)
        avg_rate = sum(p.execution_rate for p in trend) / len(trend) if trend else 0.0

        return jsonify({
            "status": "ok",
            "query_range": {
                "start_date": start_date_str,
                "end_date": end_date_str,
            },
            "summary": {
                "total_net_revenue": round(total_net, 4),
                "avg_execution_rate": round(avg_rate, 2),
                "total_days": len(trend_data),
            },
            "trend": trend_data,
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/arbitrage/resettle/<date_str>", methods=["POST"])
def resettle_arbitrage_day(date_str):
    """
    手动触发某天的结算重算（参数修正后可以重算历史）
    参数:
      - date_str: 日期，格式 YYYY-MM-DD (URL路径参数)
    返回: 重算后的结算详情
    """
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式错误，应为 YYYY-MM-DD"}), 400

    try:
        settlement = state.arbitrage_analyzer.resettle_day(date_str)
        return jsonify({
            "status": "ok",
            "message": f"日期 {date_str} 套利结算已重算完成",
            "settlement": _arbitrage_settlement_to_dict(settlement),
        })
    except Exception as e:
        return jsonify({"error": f"结算重算失败: {str(e)}"}), 500


@app.route("/api/arbitrage/alerts", methods=["GET"])
def get_arbitrage_alerts():
    """
    查询套利相关告警列表
    参数:
      - acknowledged: 是否已确认，可选 true/false，默认全部
      - limit: 返回数量限制，默认50
    """
    try:
        ack_str = request.args.get("acknowledged")
        limit = int(request.args.get("limit", 50))

        acknowledged = None
        if ack_str is not None:
            acknowledged = ack_str.lower() in ("true", "1", "yes", "y")

        alerts = state.arbitrage_analyzer.get_alerts(acknowledged=acknowledged, limit=limit)

        return jsonify({
            "status": "ok",
            "total": len(state.arbitrage_analyzer.alerts),
            "returned": len(alerts),
            "filter": {
                "acknowledged": acknowledged,
                "limit": limit,
            },
            "alerts": [
                {
                    "alert_id": a.alert_id,
                    "timestamp": a.timestamp.isoformat(),
                    "alert_type": a.alert_type,
                    "alert_level": a.alert_level,
                    "message": a.message,
                    "details": a.details,
                    "acknowledged": a.acknowledged,
                    "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
                    "acknowledged_by": a.acknowledged_by,
                }
                for a in reversed(alerts)
            ],
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/arbitrage/alerts/<alert_id>/acknowledge", methods=["POST"])
def acknowledge_arbitrage_alert(alert_id):
    """
    确认套利告警
    参数:
      - alert_id: 告警ID (URL路径参数)
    请求体 (可选):
      - acknowledged_by: 确认人标识
    """
    data = request.get_json(silent=True) or {}
    acknowledged_by = data.get("acknowledged_by")

    success = state.arbitrage_analyzer.acknowledge_alert(alert_id, acknowledged_by)
    if not success:
        return jsonify({"error": f"未找到告警或已确认: {alert_id}"}), 404

    return jsonify({
        "status": "ok",
        "message": f"套利告警 {alert_id} 已确认",
        "alert_id": alert_id,
    })


@app.route("/api/arbitrage/hourly/<date_str>", methods=["GET"])
def get_arbitrage_hourly_records(date_str):
    """
    查询指定日期的逐小时套利记录（用于调试和深入分析）
    参数:
      - date_str: 日期，格式 YYYY-MM-DD (URL路径参数)
    """
    try:
        from datetime import datetime
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "日期格式错误，应为 YYYY-MM-DD"}), 400

    records = state.arbitrage_analyzer.get_hourly_records(date_str)

    return jsonify({
        "status": "ok",
        "date": date_str,
        "total_records": len(records),
        "records": [
            {
                "timestamp": r.timestamp.isoformat(),
                "hour": r.hour,
                "tariff_period": r.tariff_period,
                "tariff_period_chinese": {
                    "valley": "谷时段",
                    "flat": "平时段",
                    "peak": "峰时段",
                }.get(r.tariff_period, "未知"),
                "planned_mode": r.planned_mode,
                "planned_mode_chinese": {
                    "active_charge": "主动充电",
                    "priority_discharge": "优先放电",
                    "normal": "常规模式",
                }.get(r.planned_mode, "未知"),
                "actual_charge_kw": round(r.actual_charge_kw, 4),
                "actual_discharge_kw": round(r.actual_discharge_kw, 4),
                "charge_from_grid_kw": round(r.charge_from_grid_kw, 4),
                "charge_from_renewable_kw": round(r.charge_from_renewable_kw, 4),
                "charge_kwh": round(r.actual_charge_kw * r.time_interval_hours, 4),
                "discharge_kwh": round(r.actual_discharge_kw * r.time_interval_hours, 4),
                "is_active_arbitrage": r.is_active_arbitrage,
                "interrupted": r.interrupted,
                "interruption_reason": r.interruption_reason,
                "time_interval_hours": r.time_interval_hours,
                "grid_buy_price": r.grid_buy_price,
                "soc_before": round(r.soc_before * 100, 2),
                "soc_after": round(r.soc_after * 100, 2),
            }
            for r in records
        ],
    })


@app.route("/api/arbitrage/config", methods=["GET"])
def get_arbitrage_config():
    """查询套利分析配置参数"""
    cfg = config.ARBITRAGE_ANALYSIS_CONFIG
    return jsonify({
        "status": "ok",
        "config": {
            "enable_arbitrage_analysis": cfg.get("enable_arbitrage_analysis", True),
            "settlement_time": f"{cfg.get('settlement_hour', 0):02d}:{cfg.get('settlement_minute', 0):02d}",
            "low_execution_rate_threshold": cfg.get("low_execution_rate_threshold", 50.0),
            "consecutive_low_days_threshold": cfg.get("consecutive_low_days_threshold", 3),
            "passive_charge_from_renewable": cfg.get("passive_charge_from_renewable", True),
            "tariff": {
                "valley_price": config.get_valley_price(),
                "peak_price": config.get_peak_price(),
                "flat_price": config.get_flat_price(),
                "valley_hours": config.GRID_TARIFF["valley"]["hours"],
                "peak_hours": config.GRID_TARIFF["peak"]["hours"],
            },
            "bess": {
                bid: {
                    "capacity_kwh": bcfg["capacity_kwh"],
                    "max_charge_power": bcfg["max_charge_power"],
                    "max_discharge_power": bcfg["max_discharge_power"],
                    "charge_efficiency": bcfg["charge_efficiency"],
                    "discharge_efficiency": bcfg["discharge_efficiency"],
                }
                for bid, bcfg in config.BESS_CONFIG.items()
            },
        },
    })


@app.route("/api/maintenance/plans", methods=["POST"])
def create_maintenance_plan():
    """
    创建维保计划
    请求体: {
        "device_type": "pv|wt|diesel|bess",        必填
        "device_id": "设备ID",                      必填
        "start_time": "ISO格式开始时间",             必填
        "end_time": "ISO格式结束时间",               必填
        "maintenance_type": "routine_inspection|preventive_maintenance|fault_repair",  必填
        "handling_mode": "full_shutdown|derated",    必填
        "derating_percent": 30.0                     derated时必填，0~100之间
    }
    """
    data = request.get_json(force=True) or {}
    required = ["device_type", "device_id", "start_time", "end_time", "maintenance_type", "handling_mode"]
    for f in required:
        if f not in data:
            return jsonify({"error": f"缺少必填字段: {f}"}), 400

    try:
        start_time = datetime.fromisoformat(data["start_time"])
        end_time = datetime.fromisoformat(data["end_time"])
    except ValueError:
        return jsonify({"error": "时间格式错误，请使用ISO格式，如 2024-06-18T10:00:00"}), 400

    derating_percent = float(data.get("derating_percent", 0.0))

    result = state.create_maintenance_plan(
        device_type=data["device_type"],
        device_id=data["device_id"],
        start_time=start_time,
        end_time=end_time,
        maintenance_type=data["maintenance_type"],
        handling_mode=data["handling_mode"],
        derating_percent=derating_percent,
    )

    if not result.get("success"):
        return jsonify({"error": result.get("error", "创建失败")}), 400

    return jsonify({
        "status": "ok",
        "message": "维保计划创建成功",
        "plan_id": result["plan_id"],
        "plan": result["plan"],
    })


@app.route("/api/maintenance/plans", methods=["GET"])
def list_maintenance_plans():
    """
    查询维保计划列表
    参数:
      - status: pending|active|completed|cancelled  可选，按状态筛选
      - device_type: pv|wt|diesel|bess              可选，按设备类型筛选
      - device_id: 设备ID                            可选，按设备ID筛选
      - limit: 返回数量，默认100
      - offset: 偏移量，默认0
    """
    status = request.args.get("status")
    device_type = request.args.get("device_type")
    device_id = request.args.get("device_id")
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit 和 offset 必须是整数"}), 400

    if status and status not in ("pending", "active", "completed", "cancelled"):
        return jsonify({"error": "status 必须是 pending|active|completed|cancelled 之一"}), 400
    if device_type and device_type not in ("pv", "wt", "diesel", "bess"):
        return jsonify({"error": "device_type 必须是 pv|wt|diesel|bess 之一"}), 400

    result = state.list_maintenance_plans(
        status=status,
        device_type=device_type,
        device_id=device_id,
        limit=limit,
        offset=offset,
    )

    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        **result,
    })


@app.route("/api/maintenance/plans/<plan_id>", methods=["GET"])
def get_maintenance_plan(plan_id):
    """查询单个维保计划详情"""
    plan = state.get_maintenance_plan(plan_id)
    if plan is None:
        return jsonify({"error": f"未找到维保计划: {plan_id}"}), 404
    return jsonify({
        "status": "ok",
        "plan": plan,
    })


@app.route("/api/maintenance/plans/<plan_id>/cancel", methods=["POST"])
def cancel_maintenance_plan(plan_id):
    """
    取消未开始的维保计划
    请求体 (可选): {
        "reason": "取消原因"
    }
    """
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "")

    result = state.cancel_maintenance_plan(plan_id, reason=reason)

    if not result.get("success"):
        return jsonify({"error": result.get("error", "取消失败")}), 400

    return jsonify({
        "status": "ok",
        "message": "维保计划已取消",
        "plan": result["plan"],
    })


@app.route("/api/maintenance/plans/<plan_id>/end", methods=["POST"])
def end_maintenance_plan_early(plan_id):
    """
    提前结束正在执行的维保计划（立即恢复设备）
    """
    result = state.end_maintenance_plan_early(plan_id)

    if not result.get("success"):
        return jsonify({"error": result.get("error", "结束失败")}), 400

    return jsonify({
        "status": "ok",
        "message": "维保计划已提前结束，设备已恢复正常",
        "plan": result["plan"],
    })


@app.route("/api/maintenance/history/<device_type>/<device_id>", methods=["GET"])
def get_device_maintenance_history(device_type, device_id):
    """
    查询某设备的维保历史
    URL参数:
      - device_type: pv|wt|diesel|bess
      - device_id: 设备ID
    Query参数:
      - limit: 返回数量，默认50
    """
    if device_type not in ("pv", "wt", "diesel", "bess"):
        return jsonify({"error": "device_type 必须是 pv|wt|diesel|bess 之一"}), 400

    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    result = state.get_device_maintenance_history(device_type, device_id, limit=limit)

    if not result.get("success"):
        return jsonify({"error": result.get("error", "查询失败")}), 400

    return jsonify(result)


@app.route("/api/maintenance/active", methods=["GET"])
def get_current_maintenance_restrictions():
    """
    查询当前所有受维保影响的设备及其限制详情
    """
    result = state.get_current_maintenance_restrictions()
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        **result,
    })


@app.route("/api/maintenance/check", methods=["POST"])
def manual_check_maintenance_plans():
    """
    手动触发维保计划状态检查（正常情况下调度会自动检查，此接口用于调试）
    """
    now = datetime.now()
    result = state.check_and_update_maintenance_plans(now)
    return jsonify({
        "status": "ok",
        "check_time": now.isoformat(),
        **result,
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
        "GET /api/alerts - 告警记录（原始列表）",
        "GET /api/alerts/active - 活跃告警列表（按级别筛选，按时间排序）",
        "GET /api/alerts/<alert_id> - 单条告警详情",
        "GET /api/alerts/<alert_id>/escalation-history - 告警升级历史时间线",
        "POST /api/alerts/<alert_id>/acknowledge - 确认单条告警",
        "POST /api/alerts/acknowledge-by-type - 批量确认同类型告警",
        "POST /api/alerts/check-escalation - 手动触发告警升级检查",
        "GET /api/alerts/statistics - 告警统计（按类型/级别聚合，24小时趋势）",
        "GET /api/duty/staff - 值班人员列表",
        "POST /api/duty/staff - 新增值班人员",
        "GET /api/duty/staff/<staff_id> - 单个值班人员详情",
        "PUT /api/duty/staff/<staff_id> - 修改值班人员配置",
        "DELETE /api/duty/staff/<staff_id> - 删除值班人员",
        "GET /api/duty/on-duty-now - 当前值班人员状态",
        "GET /api/duty/notifications - 通知队列（已发送/待发送/无人接收）",
        "POST /api/duty/notifications/process - 手动处理通知队列",
        "GET /api/source/health - 所有发电源健康评分",
        "GET /api/source/health/<type>/<id> - 单个发电源健康评分",
        "GET /api/source/health/<type>/<id>/history - 发电源健康评分历史趋势",
        "POST /api/source/maintenance - 设置发电源维护状态",
        "GET /api/backup-plans - 备用预案列表",
        "GET /api/backup-plans/<type>/<id> - 单源备用预案",
        "GET /api/fault-events - 故障事件记录",
        "GET /api/dynamic-shed/pressure - 当前供电压力指数和模式",
        "GET /api/dynamic-shed/pressure/history - 压力指数历史趋势",
        "GET /api/dynamic-shed/limits - 各群组当前动态限额",
        "GET /api/dynamic-shed/mode-history - 模式切换历史记录",
        "POST /api/dynamic-shed/mode-lock - 手动锁定/解锁模式",
        "GET /api/dynamic-shed/config - 动态限额功能配置参数",
        "PUT /api/config/tariff - 修改电价配置",
        "PUT /api/config/bess_soc - 修改SOC区间",
        "GET /api/config/all - 查看全部配置",
        "GET /api/audit/logs - 审计日志列表（支持筛选）",
        "GET /api/audit/logs/<audit_id> - 单条审计日志详情",
        "GET /api/audit/anomalies - 异常决策列表",
        "GET /api/audit/compare - 决策对比接口",
        "POST /api/report/daily - 生成日报",
        "POST /api/report/weekly - 生成周报",
        "GET /api/report/list - 报告列表（支持按类型筛选）",
        "GET /api/report/<report_id> - 单份报告详情",
        "DELETE /api/report/<report_id> - 删除报告",
        "GET /api/health - 健康检查",
        "GET /api/dual-strategy/params - 查询主策略和影子策略参数配置",
        "PUT /api/dual-strategy/shadow-params - 修改影子策略参数",
        "GET /api/dual-strategy/progress - 查询当前对比进度和累计统计",
        "GET /api/dual-strategy/switch-history - 查询策略切换历史记录",
        "POST /api/dual-strategy/evaluate - 手动触发立即评估对比",
        "POST /api/dual-strategy/force-switch - 手动强制切换影子为主策略",
        "GET /api/arbitrage/settlement/<date> - 查询指定日期的套利结算详情",
        "GET /api/arbitrage/summary - 查询日期范围内的套利汇总",
        "GET /api/arbitrage/trend - 查询套利趋势（按天净收益和执行率走势）",
        "POST /api/arbitrage/resettle/<date> - 手动触发某天的结算重算",
        "GET /api/arbitrage/alerts - 查询套利相关告警列表",
        "POST /api/arbitrage/alerts/<alert_id>/acknowledge - 确认套利告警",
        "GET /api/arbitrage/hourly/<date> - 查询指定日期的逐小时套利记录",
        "GET /api/arbitrage/config - 查询套利分析配置参数",
        "POST /api/maintenance/plans - 创建维保计划",
        "GET /api/maintenance/plans - 查询维保计划列表（按状态/设备筛选）",
        "GET /api/maintenance/plans/<plan_id> - 查询单个维保计划详情",
        "POST /api/maintenance/plans/<plan_id>/cancel - 取消未开始的维保计划",
        "POST /api/maintenance/plans/<plan_id>/end - 提前结束执行中的维保计划",
        "GET /api/maintenance/history/<device_type>/<device_id> - 查询某设备的维保历史",
        "GET /api/maintenance/active - 查询当前受维保影响的设备及限制详情",
        "POST /api/maintenance/check - 手动触发维保计划状态检查（调试用）",
    ]

def _hourly_plan_to_dict(hp):
    return {
        "hour": hp.hour,
        "forecast_load_kw": hp.forecast_load_kw,
        "forecast_pv_kw": hp.forecast_pv_kw,
        "forecast_wt_kw": hp.forecast_wt_kw,
        "tariff_period": hp.tariff_period,
        "tariff_period_chinese": {"valley": "谷时段", "flat": "平时段", "peak": "峰时段"}.get(hp.tariff_period, "未知"),
        "grid_buy_price": hp.grid_buy_price,
        "planned_charge_kw": hp.planned_charge_kw,
        "planned_discharge_kw": hp.planned_discharge_kw,
        "planned_grid_import_kw": hp.planned_grid_import_kw,
        "planned_diesel_kw": hp.planned_diesel_kw,
        "planned_cost": hp.planned_cost,
        "soc_start": hp.soc_start,
        "soc_start_percent": round(hp.soc_start * 100, 2),
        "soc_end": hp.soc_end,
        "soc_end_percent": round(hp.soc_end * 100, 2),
        "diesel_running": hp.diesel_running,
        "notes": hp.notes,
        "actual_data": hp.actual_data,
        "deviation": hp.deviation,
    }


def _plan_to_dict(plan):
    hours_list = []
    for h in range(24):
        if h in plan.hours:
            hours_list.append(_hourly_plan_to_dict(plan.hours[h]))

    total_charge = sum(h.planned_charge_kw for h in plan.hours.values())
    total_discharge = sum(h.planned_discharge_kw for h in plan.hours.values())
    total_grid = sum(h.planned_grid_import_kw for h in plan.hours.values())
    total_diesel = sum(h.planned_diesel_kw for h in plan.hours.values())

    valley_hours = [h for h in plan.hours.values() if h.tariff_period == "valley"]
    peak_hours = [h for h in plan.hours.values() if h.tariff_period == "peak"]
    flat_hours = [h for h in plan.hours.values() if h.tariff_period == "flat"]

    return {
        "plan_id": plan.plan_id,
        "plan_date": plan.plan_date,
        "generated_at": plan.generated_at.isoformat(),
        "forecast_id": plan.forecast_id,
        "initial_soc": plan.initial_soc,
        "initial_soc_percent": round(plan.initial_soc * 100, 2),
        "total_planned_cost": plan.total_planned_cost,
        "version": plan.version,
        "parent_plan_id": plan.parent_plan_id,
        "generated_by": plan.generated_by,
        "generated_by_chinese": {"auto": "自动生成", "manual": "手动生成"}.get(plan.generated_by, plan.generated_by),
        "note": plan.note,
        "is_active": plan.is_active,
        "activated_at": plan.activated_at.isoformat() if plan.activated_at else None,
        "actual_total_cost": plan.actual_total_cost,
        "completed": plan.completed,
        "summary": {
            "total_planned_charge_kwh": round(total_charge, 4),
            "total_planned_discharge_kwh": round(total_discharge, 4),
            "total_planned_grid_import_kwh": round(total_grid, 4),
            "total_planned_diesel_kwh": round(total_diesel, 4),
            "valley_hour_count": len(valley_hours),
            "peak_hour_count": len(peak_hours),
            "flat_hour_count": len(flat_hours),
            "valley_total_grid_import_kwh": round(sum(h.planned_grid_import_kw for h in valley_hours), 4),
            "peak_total_grid_import_kwh": round(sum(h.planned_grid_import_kw for h in peak_hours), 4),
            "flat_total_grid_import_kwh": round(sum(h.planned_grid_import_kw for h in flat_hours), 4),
            "valley_avg_grid_import_kw": round(sum(h.planned_grid_import_kw for h in valley_hours) / max(len(valley_hours), 1), 4),
            "peak_avg_grid_import_kw": round(sum(h.planned_grid_import_kw for h in peak_hours) / max(len(peak_hours), 1), 4),
        },
        "hours": hours_list,
    }


@app.route("/api/day-ahead/forecast", methods=["POST"])
def submit_day_ahead_forecast():
    """
    提交次日预测并生成经济调度计划
    请求体: {
        "forecast_date": "2024-06-22",
        "initial_soc": 0.5,  (可选，默认使用当前电池SOC)
        "hours": [
            {
                "hour": 0,
                "forecast_load_kw": 150.0,
                "forecast_pv_kw": 0.0,
                "forecast_wt_kw": 20.0
            },
            ...  (共24小时数据)
        ]
    }
    """
    try:
        data = request.get_json(force=True) or {}

        if "forecast_date" not in data:
            return jsonify({"error": "缺少必填字段: forecast_date"}), 400
        if "hours" not in data or not isinstance(data["hours"], list):
            return jsonify({"error": "缺少必填字段: hours (数组形式，共24小时)"}), 400

        forecast_date = data["forecast_date"]
        hours_data = data["hours"]
        initial_soc = data.get("initial_soc")
        if initial_soc is not None:
            initial_soc = float(initial_soc)

        result = state.submit_day_ahead_forecast(
            forecast_date=forecast_date,
            hours_data=hours_data,
            initial_soc=initial_soc,
        )

        if not result["success"]:
            return jsonify({"error": result.get("error", "生成计划失败")}), 400

        plan = state.get_day_ahead_plan(result["plan_id"])

        return jsonify({
            "status": "ok",
            "message": "日前经济调度计划生成成功",
            "forecast_id": result["forecast_id"],
            "plan_id": result["plan_id"],
            "plan_date": result["plan_date"],
            "total_planned_cost": result["total_planned_cost"],
            "initial_soc": result["initial_soc"],
            "initial_soc_percent": round(result["initial_soc"] * 100, 2),
            "version": result["version"],
            "replaced_plan_id": result.get("replaced_plan_id"),
            "plan": _plan_to_dict(plan) if plan else None,
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/day-ahead/plan/active", methods=["GET"])
def get_active_day_ahead_plan():
    """
    查询当前生效的日前计划（整天24时段明细）
    """
    plan = state.get_active_day_ahead_plan()
    if plan is None:
        return jsonify({
            "status": "ok",
            "plan_exists": False,
            "message": "当前无生效的日前计划",
            "plan": None,
        })

    return jsonify({
        "status": "ok",
        "plan_exists": True,
        "plan": _plan_to_dict(plan),
    })


@app.route("/api/day-ahead/plan/<plan_id>", methods=["GET"])
def get_day_ahead_plan_detail(plan_id):
    """
    查询指定计划的详情（整天24时段明细）
    """
    plan = state.get_day_ahead_plan(plan_id)
    if plan is None:
        return jsonify({"error": f"未找到计划: {plan_id}"}), 404

    return jsonify({
        "status": "ok",
        "plan": _plan_to_dict(plan),
    })


@app.route("/api/day-ahead/plan/<plan_id>/hour/<int:hour>", methods=["GET"])
def get_day_ahead_plan_hour(plan_id, hour):
    """
    查询某时段的计划详情
    参数: hour (0-23)
    """
    if not (0 <= hour <= 23):
        return jsonify({"error": "hour 必须在 0-23 之间"}), 400

    plan_hour = state.get_day_ahead_plan_hour(plan_id, hour)
    if plan_hour is None:
        return jsonify({"error": f"未找到计划 {plan_id} 的时段 {hour} 数据"}), 404

    return jsonify({
        "status": "ok",
        "plan_id": plan_id,
        "hour": hour,
        "plan": _hourly_plan_to_dict(plan_hour),
    })


@app.route("/api/day-ahead/plans", methods=["GET"])
def list_day_ahead_plans():
    """
    查询日前计划列表
    参数: plan_date (可选，按日期筛选，格式 YYYY-MM-DD)
    """
    plan_date = request.args.get("plan_date")
    plans = state.list_day_ahead_plans(plan_date)

    result = []
    for plan in plans:
        result.append({
            "plan_id": plan.plan_id,
            "plan_date": plan.plan_date,
            "generated_at": plan.generated_at.isoformat(),
            "total_planned_cost": plan.total_planned_cost,
            "version": plan.version,
            "is_active": plan.is_active,
            "completed": plan.completed,
            "generated_by": plan.generated_by,
            "generated_by_chinese": {"auto": "自动生成", "manual": "手动生成"}.get(plan.generated_by, plan.generated_by),
            "note": plan.note,
            "parent_plan_id": plan.parent_plan_id,
        })

    return jsonify({
        "status": "ok",
        "total": len(result),
        "plan_date": plan_date,
        "plans": result,
    })


@app.route("/api/day-ahead/forecasts", methods=["GET"])
def list_day_ahead_forecasts():
    """
    查询预测数据列表
    参数: forecast_date (可选，按日期筛选，格式 YYYY-MM-DD)
    """
    forecast_date = request.args.get("forecast_date")
    forecasts = state.list_day_ahead_forecasts(forecast_date)

    result = []
    for fc in forecasts:
        hours_summary = []
        for fh in fc.hours:
            hours_summary.append({
                "hour": fh.hour,
                "forecast_load_kw": fh.forecast_load_kw,
                "forecast_pv_kw": fh.forecast_pv_kw,
                "forecast_wt_kw": fh.forecast_wt_kw,
            })
        result.append({
            "forecast_id": fc.forecast_id,
            "forecast_date": fc.forecast_date,
            "submitted_at": fc.submitted_at.isoformat(),
            "initial_soc": fc.initial_soc,
            "status": fc.status,
            "activated_at": fc.activated_at.isoformat() if fc.activated_at else None,
            "hours": hours_summary,
        })

    return jsonify({
        "status": "ok",
        "total": len(result),
        "forecast_date": forecast_date,
        "forecasts": result,
    })


@app.route("/api/day-ahead/plan/<plan_id>/actual-data", methods=["POST"])
def submit_hourly_actual_data(plan_id):
    """
    提交时段实际运行数据，自动检测偏差并触发滚动校正
    请求体: {
        "hour": 10,
        "actual_load_kw": 200.0,
        "actual_pv_kw": 80.0,
        "actual_wt_kw": 15.0,
        "actual_charge_kw": 0.0,
        "actual_discharge_kw": 50.0,
        "actual_grid_import_kw": 55.0,
        "actual_diesel_kw": 0.0,
        "actual_cost": 66.0,
        "soc_end": 0.45
    }
    """
    try:
        data = request.get_json(force=True) or {}

        if "hour" not in data:
            return jsonify({"error": "缺少必填字段: hour"}), 400

        hour = int(data["hour"])
        if not (0 <= hour <= 23):
            return jsonify({"error": "hour 必须在 0-23 之间"}), 400

        result = state.submit_hourly_actual_data(
            plan_id=plan_id,
            hour=hour,
            actual_data=data,
        )

        if not result["success"]:
            return jsonify({"error": result.get("error", "提交实际数据失败")}), 400

        response = {
            "status": "ok",
            "message": "实际数据已提交",
            "hour": result["hour"],
            "plan_id": result["plan_id"],
            "deviation_detected": result["deviation_detected"],
        }

        if "correction" in result:
            response["correction"] = result["correction"]
            response["new_plan"] = result["new_plan"]

        return jsonify(response)
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/day-ahead/corrections", methods=["GET"])
def get_correction_events():
    """
    查询校正事件历史
    参数:
      - plan_id: 可选，按计划筛选
      - limit: 可选，返回数量限制，默认100
    """
    try:
        plan_id = request.args.get("plan_id")
        limit = int(request.args.get("limit", 100))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    events = state.get_correction_events(plan_id=plan_id, limit=limit)

    result = []
    for event in events:
        result.append({
            "event_id": event.event_id,
            "plan_id": event.plan_id,
            "triggered_at": event.triggered_at.isoformat(),
            "triggered_by_hour": event.triggered_by_hour,
            "trigger_reason": event.trigger_reason,
            "deviation_details": event.deviation_details,
            "old_plan_id": event.old_plan_id,
            "new_plan_id": event.new_plan_id,
            "corrected_hours": event.corrected_hours,
        })

    return jsonify({
        "status": "ok",
        "total": len(result),
        "plan_id": plan_id,
        "limit": limit,
        "corrections": result,
    })


@app.route("/api/day-ahead/plan/<plan_id>/evaluate", methods=["POST"])
def generate_plan_evaluation(plan_id):
    """
    生成并查询计划执行评估报告
    对比计划执行情况：计划总成本vs实际总成本、各时段偏差、触发了几次滚动校正、预测准确度
    """
    result = state.generate_plan_evaluation_report(plan_id)

    if not result["success"]:
        return jsonify({"error": result.get("error", "生成评估报告失败")}), 400

    return jsonify({
        "status": "ok",
        "message": "计划执行评估报告生成成功",
        "report": {
            "report_id": result["report_id"],
            "plan_id": result["plan_id"],
            "plan_date": result["plan_date"],
            "generated_at": datetime.now().isoformat(),
            "total_planned_cost": result["total_planned_cost"],
            "total_actual_cost": result["total_actual_cost"],
            "cost_deviation_percent": result["cost_deviation_percent"],
            "avg_load_forecast_error_percent": result["avg_load_forecast_error_percent"],
            "correction_count": result["correction_count"],
            "correction_events": result["correction_events"],
            "hourly_deviations": result["hourly_deviations"],
            "details": result["details"],
        },
    })


@app.route("/api/day-ahead/evaluations", methods=["GET"])
def list_evaluation_reports():
    """
    查询评估报告列表
    参数: plan_id (可选，按计划筛选)
    """
    plan_id = request.args.get("plan_id")
    reports = state.list_evaluation_reports(plan_id)

    result = []
    for report in reports:
        result.append({
            "report_id": report.report_id,
            "plan_id": report.plan_id,
            "plan_date": report.plan_date,
            "generated_at": report.generated_at.isoformat(),
            "total_planned_cost": report.total_planned_cost,
            "total_actual_cost": report.total_actual_cost,
            "cost_deviation_percent": report.cost_deviation_percent,
            "avg_load_forecast_error_percent": report.avg_load_forecast_error_percent,
            "correction_count": report.correction_count,
        })

    return jsonify({
        "status": "ok",
        "total": len(result),
        "plan_id": plan_id,
        "reports": result,
    })


@app.route("/api/day-ahead/plan/<plan_id>/regenerate", methods=["POST"])
def regenerate_day_ahead_plan(plan_id):
    """
    手动重新生成计划
    请求体: {
        "start_hour": 0,  (可选，默认0，从指定时段开始重算)
        "initial_soc": 0.5  (可选，默认使用计划中start_hour的起始SOC)
    }
    """
    try:
        data = request.get_json(force=True) or {}
        start_hour = int(data.get("start_hour", 0))
        initial_soc = data.get("initial_soc")
        if initial_soc is not None:
            initial_soc = float(initial_soc)

        if not (0 <= start_hour <= 23):
            return jsonify({"error": "start_hour 必须在 0-23 之间"}), 400

        result = state.regenerate_day_ahead_plan(
            plan_id=plan_id,
            start_hour=start_hour,
            initial_soc=initial_soc,
        )

        if not result["success"]:
            return jsonify({"error": result.get("error", "重新生成计划失败")}), 400

        new_plan = state.get_day_ahead_plan(result["new_plan_id"])

        return jsonify({
            "status": "ok",
            "message": "计划已手动重新生成",
            "old_plan_id": result["old_plan_id"],
            "new_plan_id": result["new_plan_id"],
            "new_plan_version": result["new_plan_version"],
            "start_hour": result["start_hour"],
            "initial_soc": result["initial_soc"],
            "initial_soc_percent": round(result["initial_soc"] * 100, 2),
            "total_planned_cost": result["total_planned_cost"],
            "note": result["note"],
            "became_active": result["became_active"],
            "new_plan": _plan_to_dict(new_plan) if new_plan else None,
        })
    except ValueError as e:
        return jsonify({"error": f"参数错误: {str(e)}"}), 400


@app.route("/api/day-ahead/forecast/<forecast_id>", methods=["GET"])
def get_day_ahead_forecast_detail(forecast_id):
    """
    查询指定预测数据的详情
    """
    forecast = state.get_day_ahead_forecast(forecast_id)
    if forecast is None:
        return jsonify({"error": f"未找到预测数据: {forecast_id}"}), 404

    hours = []
    for fh in forecast.hours:
        period, price = config.get_grid_price_for_hour(fh.hour) if hasattr(config, 'get_grid_price_for_hour') else config.get_tariff_period(fh.hour), config.GRID_TARIFF[config.get_tariff_period(fh.hour)]["price"]
        if isinstance(period, tuple):
            period, price = period[0], config.GRID_TARIFF[period]["price"]
        hours.append({
            "hour": fh.hour,
            "forecast_load_kw": fh.forecast_load_kw,
            "forecast_pv_kw": fh.forecast_pv_kw,
            "forecast_wt_kw": fh.forecast_wt_kw,
            "total_renewable_kw": fh.forecast_pv_kw + fh.forecast_wt_kw,
            "net_load_kw": max(0.0, fh.forecast_load_kw - fh.forecast_pv_kw - fh.forecast_wt_kw),
            "tariff_period": config.get_tariff_period(fh.hour),
            "tariff_period_chinese": {"valley": "谷时段", "flat": "平时段", "peak": "峰时段"}.get(config.get_tariff_period(fh.hour), "未知"),
            "grid_buy_price": config.GRID_TARIFF[config.get_tariff_period(fh.hour)]["price"],
        })

    return jsonify({
        "status": "ok",
        "forecast": {
            "forecast_id": forecast.forecast_id,
            "forecast_date": forecast.forecast_date,
            "submitted_at": forecast.submitted_at.isoformat(),
            "initial_soc": forecast.initial_soc,
            "initial_soc_percent": round(forecast.initial_soc * 100, 2),
            "status": forecast.status,
            "activated_at": forecast.activated_at.isoformat() if forecast.activated_at else None,
            "hours": hours,
        },
    })


@app.route("/api/reactive-power/status", methods=["GET"])
def get_reactive_power_status():
    """
    查询当前并网点功率因数和无功状态
    返回: 总有功、总无功、当前功率因数、已投入电容组数等
    """
    status = state.reactive_power_manager.get_status()
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "reactive_power_status": status,
    })


@app.route("/api/reactive-power/switch-history", methods=["GET"])
def get_capacitor_switch_history():
    """
    查询电容投切历史
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 500))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    history = state.reactive_power_manager.get_switch_history(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(state.reactive_power_manager.reactive_state.switch_history),
        "returned": len(history),
        "limit": limit,
        "history": history,
    })


@app.route("/api/reactive-power/assessment-records", methods=["GET"])
def get_power_factor_assessment_records():
    """
    查询功率因数考核记录
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 500))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    records = state.reactive_power_manager.get_assessment_records(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(state.reactive_power_manager.reactive_state.assessment_records),
        "returned": len(records),
        "limit": limit,
        "records": records,
    })


@app.route("/api/reactive-power/limited-events", methods=["GET"])
def get_compensation_limited_events():
    """
    查询补偿受限事件
    参数: limit (可选，默认50)
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 500))
    except ValueError:
        return jsonify({"error": "limit 必须是整数"}), 400

    events = state.reactive_power_manager.get_limited_events(limit=limit)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "total": len(state.reactive_power_manager.reactive_state.limited_events),
        "returned": len(events),
        "limit": limit,
        "events": events,
    })


@app.route("/api/reactive-power/monthly-summary", methods=["GET"])
def get_power_factor_monthly_summary():
    """
    查询功率因数月度考核汇总
    参数: year_month (可选，格式YYYY-MM，默认当月)
    """
    year_month = request.args.get("year_month")
    summary = state.reactive_power_manager.get_monthly_summary(year_month=year_month)
    return jsonify({
        "status": "ok",
        "query_time": datetime.now().isoformat(),
        "summary": summary,
    })


@app.route("/api/reactive-power/manual-switch", methods=["POST"])
def manual_capacitor_switch():
    """
    手动投切指定数量的电容组（覆盖自动逻辑，用于检修测试）
    请求体: {
        "target_groups": 3  (目标投入的电容组数，0-6)
    }
    """
    data = request.get_json(force=True) or {}
    if "target_groups" not in data:
        return jsonify({"error": "缺少必填字段: target_groups"}), 400

    try:
        target_groups = int(data["target_groups"])
    except (ValueError, TypeError):
        return jsonify({"error": "target_groups 必须是整数"}), 400

    total_groups = config.REACTIVE_POWER_CONFIG["capacitor_total_groups"]
    if target_groups < 0 or target_groups > total_groups:
        return jsonify({"error": f"target_groups 必须在 0-{total_groups} 之间"}), 400

    result = state.reactive_power_manager.manual_switch(target_groups)

    if result["success"]:
        switch_event = result.get("switch_event")
        return jsonify({
            "status": "ok",
            "message": result["message"],
            "groups_online": result["groups_online"],
            "switch_event": {
                "event_id": switch_event.event_id,
                "timestamp": switch_event.timestamp.isoformat(),
                "groups_before": switch_event.groups_before,
                "groups_after": switch_event.groups_after,
                "switch_delta": switch_event.switch_delta,
                "pf_before": round(switch_event.pf_before, 4),
                "pf_after": round(switch_event.pf_after, 4),
                "reason": switch_event.reason,
                "is_manual": switch_event.is_manual,
            } if switch_event else None,
            "current_status": state.reactive_power_manager.get_status(),
        })
    else:
        return jsonify({
            "status": "error",
            "message": result["message"],
            "groups_online": result["groups_online"],
        }), 400


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
    print("日前计划: http://127.0.0.1:5001/api/day-ahead/plan/active")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5001, debug=False)
