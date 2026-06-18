from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import asdict

import config
from models import (
    AuditLog, InputSnapshot, DecisionBranch, OutputSummary,
    AnomalyMarker, MicrogridState, DispatchDecision
)


class AuditBuilder:
    def __init__(self, state: MicrogridState, dispatch_id: str, now: datetime):
        self.state = state
        self.dispatch_id = dispatch_id
        self.now = now
        self.decision_branches: List[DecisionBranch] = []
        self.reasoning_chain: List[str] = []

    def capture_input_snapshot(self) -> InputSnapshot:
        pv_output = {}
        for sid in config.PV_CONFIG:
            r = self.state.pv_reports.get(sid)
            pv_output[sid] = max(0.0, r.power_kw) if (r and r.available) else 0.0

        wt_output = {}
        for sid in config.WT_CONFIG:
            r = self.state.wt_reports.get(sid)
            wt_output[sid] = max(0.0, r.power_kw) if (r and r.available) else 0.0

        diesel_available = {}
        for sid in config.DIESEL_CONFIG:
            r = self.state.diesel_reports.get(sid)
            diesel_available[sid] = r.available if r else False

        bess_soc = {}
        for bid in config.BESS_CONFIG:
            bess_soc[bid] = self.state.bess_state[bid].soc

        tariff_period = config.get_tariff_period(self.now.hour)
        grid_buy_price = config.GRID_TARIFF[tariff_period]["price"]

        current_plan = self.state.get_current_hour_plan(self.now)
        storage_strategy_active = current_plan is not None and current_plan.active and not current_plan.abnormal
        storage_mode = current_plan.mode if storage_strategy_active else "normal"

        source_health_status = {}
        for health_key, hs in self.state.source_health.items():
            source_health_status[health_key] = hs.status

        active_backup_plans = [p.plan_id for p in self.state.get_active_backup_plans()]

        return InputSnapshot(
            pv_output=pv_output,
            wt_output=wt_output,
            diesel_available=diesel_available,
            load_kw=self.state.get_load_kw(),
            bess_soc=bess_soc,
            grid_buy_price=grid_buy_price,
            feed_in_price=config.FEED_IN_TARIFF,
            tariff_period=tariff_period,
            hour=self.now.hour,
            storage_strategy_active=storage_strategy_active,
            storage_mode=storage_mode,
            demand_response_active=False,
            active_backup_plans=active_backup_plans,
            source_health_status=source_health_status,
        )

    def add_branch(self, branch_name: str, decision: bool, reason: str, details: Dict[str, Any] = None):
        branch = DecisionBranch(
            branch_name=branch_name,
            decision=decision,
            reason=reason,
            details=details or {},
        )
        self.decision_branches.append(branch)
        decision_text = "是" if decision else "否"
        self.reasoning_chain.append(f"[{branch_name}] {decision_text} - {reason}")

    def build_output_summary(self, decision: DispatchDecision) -> OutputSummary:
        total_pv = sum(decision.pv_output.values())
        total_wt = sum(decision.wt_output.values())
        total_diesel = sum(decision.diesel_output.values())
        total_bess_discharge = sum(
            ba["discharge_kw"] for ba in decision.bess_action.values()
        )

        load_coverage_ratio = (
            decision.load_served_kw / decision.load_served_kw
            if (decision.load_served_kw + decision.load_shed_kw) > 0
            else 1.0
        )

        cost_breakdown = {}
        diesel_gen_cost = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"]
        time_interval_hours = 1.0 / 60.0

        grid_cost = decision.grid_import_kw * time_interval_hours * decision.grid_buy_price
        diesel_cost = total_diesel * time_interval_hours * diesel_gen_cost
        feedin_revenue = decision.grid_export_kw * time_interval_hours * config.FEED_IN_TARIFF

        cost_breakdown["grid_import_cost"] = round(grid_cost, 4)
        cost_breakdown["diesel_generation_cost"] = round(diesel_cost, 4)
        cost_breakdown["feed_in_revenue"] = round(-feedin_revenue, 4)

        return OutputSummary(
            load_served_kw=decision.load_served_kw,
            load_shed_kw=decision.load_shed_kw,
            load_coverage_ratio=round(load_coverage_ratio, 4),
            total_cost=round(decision.cost, 4),
            pv_share_kw=round(total_pv, 2),
            wt_share_kw=round(total_wt, 2),
            diesel_share_kw=round(total_diesel, 2),
            bess_discharge_kw=round(total_bess_discharge, 2),
            grid_import_kw=round(decision.grid_import_kw, 2),
            grid_export_kw=round(decision.grid_export_kw, 2),
            cost_breakdown=cost_breakdown,
        )

    def build(self, decision: DispatchDecision, audit_id: str) -> AuditLog:
        input_snapshot = self.capture_input_snapshot()
        output_summary = self.build_output_summary(decision)

        return AuditLog(
            audit_id=audit_id,
            dispatch_id=self.dispatch_id,
            timestamp=self.now,
            input_snapshot=input_snapshot,
            decision_branches=self.decision_branches,
            output_summary=output_summary,
            reasoning_chain=self.reasoning_chain.copy(),
        )


class AnomalyDetector:
    def __init__(self, state: MicrogridState):
        self.state = state

    def detect_all(self, audit_log: AuditLog) -> List[AnomalyMarker]:
        anomalies = []

        anomaly_checks = [
            self._check_cost_volatility,
            self._check_consecutive_load_shedding,
            self._check_battery_over_discharge,
            self._check_diesel_in_valley_hours,
        ]

        for check_func in anomaly_checks:
            result = check_func(audit_log)
            if result:
                anomalies.append(result)

        return anomalies

    def _check_cost_volatility(self, audit_log: AuditLog) -> Optional[AnomalyMarker]:
        if len(self.state.audit_logs) < 1:
            return None

        current_cost = audit_log.output_summary.total_cost
        current_hour = audit_log.input_snapshot.hour

        prev_same_hour = None
        for log in reversed(self.state.audit_logs):
            if log.input_snapshot.hour == current_hour and log.timestamp.date() == audit_log.timestamp.date():
                prev_same_hour = log
                break

        if prev_same_hour is None or prev_same_hour.output_summary.total_cost <= 0:
            return None

        prev_cost = prev_same_hour.output_summary.total_cost
        ratio = current_cost / prev_cost
        if ratio >= 2.0:
            return AnomalyMarker(
                anomaly_type="COST_VOLATILITY",
                severity="high",
                description=f"当前小时成本波动异常，成本({current_cost:.2f}元)是上一次同时段({prev_cost:.2f}元)的{ratio:.1f}倍",
                details={
                    "current_cost": current_cost,
                    "previous_cost": prev_cost,
                    "ratio": round(ratio, 2),
                    "threshold": 2.0,
                },
            )
        return None

    def _check_consecutive_load_shedding(self, audit_log: AuditLog) -> Optional[AnomalyMarker]:
        if audit_log.output_summary.load_shed_kw <= 0:
            return None

        consecutive_count = 1

        for log in reversed(self.state.audit_logs):
            if log.output_summary.load_shed_kw > 0:
                consecutive_count += 1
            else:
                break

        if consecutive_count >= 3:
            return AnomalyMarker(
                anomaly_type="CONSECUTIVE_LOAD_SHEDDING",
                severity="critical",
                description=f"连续{consecutive_count}次调度出现甩负荷，当前甩负荷{audit_log.output_summary.load_shed_kw:.2f}kW",
                details={
                    "consecutive_count": consecutive_count,
                    "current_shed_kw": audit_log.output_summary.load_shed_kw,
                    "threshold": 3,
                    "note": "连续次数从审计功能启用后开始计数",
                },
            )
        return None

    def _check_battery_over_discharge(self, audit_log: AuditLog) -> Optional[AnomalyMarker]:
        tariff_period = audit_log.input_snapshot.tariff_period
        if tariff_period == "valley":
            return None

        bes_id = list(config.BESS_CONFIG.keys())[0]
        soc_before = audit_log.input_snapshot.bess_soc.get(bes_id, 0)
        bess_action = None

        for d in reversed(self.state.dispatch_history):
            if d.timestamp == audit_log.timestamp:
                bess_action = d.bess_action.get(bes_id, {})
                break

        if bess_action is None:
            return None

        soc_after = bess_action.get("soc_after", soc_before)

        if soc_before > 0.8 and soc_after < 0.3:
            return AnomalyMarker(
                anomaly_type="BATTERY_OVER_DISCHARGE",
                severity="high",
                description=f"电池在非谷时段过度放电，SOC从{soc_before*100:.1f}%降至{soc_after*100:.1f}%",
                details={
                    "bes_id": bes_id,
                    "soc_before": soc_before,
                    "soc_after": soc_after,
                    "soc_drop": soc_before - soc_after,
                    "tariff_period": tariff_period,
                    "threshold_high": 0.8,
                    "threshold_low": 0.3,
                },
            )
        return None

    def _check_diesel_in_valley_hours(self, audit_log: AuditLog) -> Optional[AnomalyMarker]:
        tariff_period = audit_log.input_snapshot.tariff_period
        if tariff_period != "valley":
            return None

        diesel_output = audit_log.output_summary.diesel_share_kw
        if diesel_output > 0:
            return AnomalyMarker(
                anomaly_type="DIESEL_IN_VALLEY_HOURS",
                severity="medium",
                description=f"柴油机在谷时段被启动，出力{diesel_output:.2f}kW，不符合经济性原则",
                details={
                    "diesel_output_kw": diesel_output,
                    "tariff_period": tariff_period,
                    "grid_buy_price": audit_log.input_snapshot.grid_buy_price,
                    "diesel_gen_cost": config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"],
                },
            )
        return None


class DecisionComparator:
    @staticmethod
    def compare_audits(audit1: AuditLog, audit2: AuditLog) -> Dict[str, Any]:
        input_diffs = DecisionComparator._compare_inputs(
            audit1.input_snapshot, audit2.input_snapshot
        )
        output_diffs = DecisionComparator._compare_outputs(
            audit1.output_summary, audit2.output_summary
        )
        causal_analysis = DecisionComparator._analyze_causality(
            audit1, audit2, input_diffs, output_diffs
        )

        return {
            "audit1_id": audit1.audit_id,
            "audit2_id": audit2.audit_id,
            "time_difference_minutes": round(
                abs((audit2.timestamp - audit1.timestamp).total_seconds() / 60.0),
                2,
            ),
            "input_differences": input_diffs,
            "output_differences": output_diffs,
            "causal_analysis": causal_analysis,
        }

    @staticmethod
    def _compare_inputs(input1: InputSnapshot, input2: InputSnapshot) -> Dict[str, Any]:
        diffs = {}
        input1_dict = asdict(input1)
        input2_dict = asdict(input2)

        for key in input1_dict.keys():
            val1 = input1_dict[key]
            val2 = input2_dict[key]

            if isinstance(val1, dict):
                dict_diffs = {}
                for k in set(val1.keys()) | set(val2.keys()):
                    v1 = val1.get(k)
                    v2 = val2.get(k)
                    if v1 != v2:
                        dict_diffs[k] = {"value1": v1, "value2": v2, "change": DecisionComparator._calc_change(v1, v2)}
                if dict_diffs:
                    diffs[key] = dict_diffs
            elif isinstance(val1, list):
                if set(val1) != set(val2):
                    diffs[key] = {"value1": val1, "value2": val2}
            else:
                if val1 != val2:
                    diffs[key] = {"value1": val1, "value2": val2, "change": DecisionComparator._calc_change(val1, val2)}

        return diffs

    @staticmethod
    def _compare_outputs(output1: OutputSummary, output2: OutputSummary) -> Dict[str, Any]:
        diffs = {}
        output1_dict = asdict(output1)
        output2_dict = asdict(output2)

        for key in output1_dict.keys():
            val1 = output1_dict[key]
            val2 = output2_dict[key]

            if isinstance(val1, dict):
                dict_diffs = {}
                for k in set(val1.keys()) | set(val2.keys()):
                    v1 = val1.get(k)
                    v2 = val2.get(k)
                    if v1 != v2:
                        dict_diffs[k] = {"value1": v1, "value2": v2, "change": DecisionComparator._calc_change(v1, v2)}
                if dict_diffs:
                    diffs[key] = dict_diffs
            else:
                if val1 != val2:
                    diffs[key] = {"value1": val1, "value2": val2, "change": DecisionComparator._calc_change(val1, val2)}

        return diffs

    @staticmethod
    def _calc_change(v1, v2):
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            if v1 == 0:
                return {"absolute": v2 - v1, "percentage": None if v2 == 0 else "N/A (from zero)"}
            return {
                "absolute": round(v2 - v1, 4),
                "percentage": f"{round((v2 - v1) / v1 * 100, 1)}%",
            }
        return None

    @staticmethod
    def _analyze_causality(audit1: AuditLog, audit2: AuditLog,
                           input_diffs: Dict, output_diffs: Dict) -> List[Dict[str, Any]]:
        causal_links = []

        significant_input_changes = []
        for key, diff in input_diffs.items():
            if isinstance(diff, dict) and "change" in diff and isinstance(diff["change"], dict):
                abs_change = abs(diff["change"].get("absolute", 0))
                if key in ["load_kw", "grid_buy_price"]:
                    if abs_change >= 10:
                        significant_input_changes.append((key, diff))
                elif key in ["pv_output", "wt_output"]:
                    for src, src_diff in diff.items():
                        if isinstance(src_diff, dict) and "change" in src_diff:
                            src_abs = abs(src_diff["change"].get("absolute", 0))
                            if src_abs >= 10:
                                significant_input_changes.append((f"{key}.{src}", src_diff))

        significant_output_changes = []
        for key, diff in output_diffs.items():
            if isinstance(diff, dict) and "change" in diff and isinstance(diff["change"], dict):
                abs_change = abs(diff["change"].get("absolute", 0))
                if key in ["grid_import_kw", "diesel_share_kw", "bess_discharge_kw", "load_shed_kw", "total_cost"]:
                    if abs_change >= 5:
                        significant_output_changes.append((key, diff))

        for input_key, input_diff in significant_input_changes:
            for output_key, output_diff in significant_output_changes:
                explanation = DecisionComparator._generate_causal_explanation(
                    input_key, input_diff, output_key, output_diff
                )
                if explanation:
                    causal_links.append({
                        "input_change": input_key,
                        "output_change": output_key,
                        "input_details": input_diff,
                        "output_details": output_diff,
                        "explanation": explanation,
                    })

        return causal_links

    @staticmethod
    def _generate_causal_explanation(input_key: str, input_diff: Dict,
                                     output_key: str, output_diff: Dict) -> Optional[str]:
        input_change = input_diff.get("change", {})
        output_change = output_diff.get("change", {})
        input_abs = input_change.get("absolute", 0) if isinstance(input_change, dict) else 0
        output_abs = output_change.get("absolute", 0) if isinstance(output_change, dict) else 0

        explanations = []

        if input_key == "load_kw" and input_abs > 0:
            if output_key == "grid_import_kw" and output_abs > 0:
                explanations.append(f"负荷从{input_diff['value1']:.1f}kW增加到{input_diff['value2']:.1f}kW，导致购电量从{output_diff['value1']:.1f}kW增加到{output_diff['value2']:.1f}kW")
            elif output_key == "bess_discharge_kw" and output_abs > 0:
                explanations.append(f"负荷从{input_diff['value1']:.1f}kW增加到{input_diff['value2']:.1f}kW，导致电池放电从{output_diff['value1']:.1f}kW增加到{output_diff['value2']:.1f}kW")
            elif output_key == "diesel_share_kw" and output_abs > 0:
                explanations.append(f"负荷从{input_diff['value1']:.1f}kW增加到{input_diff['value2']:.1f}kW，导致柴油机启动，出力从{output_diff['value1']:.1f}kW增加到{output_diff['value2']:.1f}kW")
            elif output_key == "load_shed_kw" and output_abs > 0:
                explanations.append(f"负荷从{input_diff['value1']:.1f}kW增加到{input_diff['value2']:.1f}kW，超出供电能力，出现甩负荷{output_abs:.1f}kW")

        if "pv_output" in input_key and input_abs < 0:
            if output_key == "grid_import_kw" and output_abs > 0:
                explanations.append(f"光伏发电下降{abs(input_abs):.1f}kW，需要增加购电{output_abs:.1f}kW来弥补缺口")
            elif output_key == "bess_discharge_kw" and output_abs > 0:
                explanations.append(f"光伏发电下降{abs(input_abs):.1f}kW，增加电池放电{output_abs:.1f}kW来替代")

        if "wt_output" in input_key and input_abs < 0:
            if output_key == "grid_import_kw" and output_abs > 0:
                explanations.append(f"风力发电下降{abs(input_abs):.1f}kW，需要增加购电{output_abs:.1f}kW来弥补缺口")

        if input_key == "grid_buy_price" and input_abs > 0:
            if output_key == "diesel_share_kw" and output_abs > 0:
                explanations.append(f"电价从{input_diff['value1']:.2f}元/kWh上涨到{input_diff['value2']:.2f}元/kWh，改用柴油发电{output_abs:.1f}kW更经济")
            elif output_key == "bess_discharge_kw" and output_abs > 0:
                explanations.append(f"电价上涨，增加电池放电{output_abs:.1f}kW以避免高价购电")

        if input_key == "grid_buy_price" and input_abs < 0:
            if output_key == "grid_import_kw" and output_abs > 0:
                explanations.append(f"电价从{input_diff['value1']:.2f}元/kWh下降到{input_diff['value2']:.2f}元/kWh，增加购电{output_abs:.1f}kW")

        if output_key == "total_cost" and output_abs != 0:
            if input_key == "load_kw" and input_abs > 0:
                explanations.append(f"负荷增加{input_abs:.1f}kW，导致总成本增加{output_abs:.2f}元")
            elif input_key == "grid_buy_price" and input_abs > 0:
                explanations.append(f"电价上涨{input_abs:.2f}元/kWh，导致总成本增加{output_abs:.2f}元")

        return explanations[0] if explanations else None
