from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import asdict
import uuid

import config
from models import (
    AuditLog, InputSnapshot, OutputSummary,
    ReplayConstraint, TheoreticalOptimalDecision,
    DecisionQualityResult, QualityScoreTrendPoint,
    LowScoreRecord, BatchReplayResult,
)


DISCHARGE_STEP_KW = 10.0
LOW_SCORE_THRESHOLD = 60.0
CONSECUTIVE_LOW_SCORE_THRESHOLD = 70.0
CONSECUTIVE_LOW_SCORE_ROUNDS = 20


class DecisionReplayEngine:
    def __init__(self, state):
        self.state = state
        self._replay_counter = 0
        self._replay_results: List[DecisionQualityResult] = []
        self._consecutive_low_score_count = 0

    def _generate_replay_id(self) -> str:
        self._replay_counter += 1
        return f"REPLAY-{self._replay_counter:08d}"

    def _extract_constraints_from_audit(self, audit: AuditLog) -> List[ReplayConstraint]:
        constraints = []

        for branch in audit.decision_branches:
            if branch.branch_name == "碳排放配额状态" and branch.decision:
                carbon_status = branch.details.get("carbon_status")
                if carbon_status in ("emergency", "exceeded"):
                    limit_ratio = branch.details.get("limit_ratio", config.CARBON_CONFIG["emergency_grid_limit_ratio"])
                    constraints.append(ReplayConstraint(
                        constraint_type="carbon_quota",
                        description=f"碳配额约束：{carbon_status}状态，购电限制在缺口的{limit_ratio*100:.0f}%",
                        details={
                            "carbon_status": carbon_status,
                            "grid_limit_ratio": limit_ratio,
                            "reason": branch.details.get("carbon_status_chinese", carbon_status),
                        }
                    ))
                elif carbon_status == "warning":
                    constraints.append(ReplayConstraint(
                        constraint_type="carbon_quota",
                        description="碳配额约束：预警状态，禁止经济性柴油机启动",
                        details={
                            "carbon_status": carbon_status,
                            "reason": branch.details.get("carbon_status_chinese", carbon_status),
                        }
                    ))

            if branch.branch_name == "需求响应状态" and branch.decision:
                constraints.append(ReplayConstraint(
                    constraint_type="demand_response",
                    description=branch.reason,
                    details=branch.details
                ))

        if audit.output_summary.load_shed_kw > 0:
            for branch in audit.decision_branches:
                if branch.branch_name == "甩负荷决策" and branch.decision:
                    reason = branch.details.get("reason", "")
                    if "carbon" in reason or "demand_response" in reason:
                        constraints.append(ReplayConstraint(
                            constraint_type="load_shed_constraint",
                            description=f"甩负荷由{reason}导致，非供电能力不足",
                            details=branch.details
                        ))

        return constraints

    def _get_max_battery_discharge(self, input_snap: InputSnapshot) -> float:
        bes_id = list(config.BESS_CONFIG.keys())[0]
        cfg = config.BESS_CONFIG[bes_id]
        soc = input_snap.bess_soc.get(bes_id, 0.0)
        soc_min = cfg["soc_min"]
        if soc <= soc_min:
            return 0.0
        time_interval = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0
        energy_avail = (soc - soc_min) * cfg["capacity_kwh"]
        max_by_energy = energy_avail * cfg["discharge_efficiency"] / time_interval
        return min(cfg["max_discharge_power"], max_by_energy)

    def _calculate_cost(self,
                        grid_import_kw: float,
                        bess_discharge_kw: float,
                        diesel_kw: float,
                        load_shed_kw: float,
                        grid_export_kw: float,
                        input_snap: InputSnapshot,
                        time_interval: float) -> Tuple[float, Dict[str, float]]:
        grid_buy_price = input_snap.grid_buy_price
        feed_in_price = input_snap.feed_in_price
        diesel_gen_cost = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"]
        shed_penalty = config.COST_ATTRIBUTION_CONFIG["load_shed_penalty_per_kwh"]

        grid_cost = grid_import_kw * time_interval * grid_buy_price
        diesel_cost = diesel_kw * time_interval * diesel_gen_cost
        feedin_revenue = grid_export_kw * time_interval * feed_in_price
        shed_penalty_cost = load_shed_kw * time_interval * shed_penalty

        total_cost = grid_cost + diesel_cost + shed_penalty_cost - feedin_revenue

        breakdown = {
            "grid_import_cost": round(grid_cost, 4),
            "diesel_generation_cost": round(diesel_cost, 4),
            "feed_in_revenue": round(-feedin_revenue, 4),
            "load_shed_penalty": round(shed_penalty_cost, 4),
        }

        return total_cost, breakdown

    def _find_theoretical_optimal(self,
                                   input_snap: InputSnapshot,
                                   constraints: List[ReplayConstraint]) -> TheoreticalOptimalDecision:
        total_pv = sum(input_snap.pv_output.values())
        total_wt = sum(input_snap.wt_output.values())
        total_renewable = total_pv + total_wt
        load_kw = input_snap.load_kw
        time_interval = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0

        grid_buy_price = input_snap.grid_buy_price
        diesel_gen_cost = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"]

        max_discharge = self._get_max_battery_discharge(input_snap)

        carbon_limit_ratio = None
        diesel_prohibited = False
        for c in constraints:
            if c.constraint_type == "carbon_quota":
                if "grid_limit_ratio" in c.details:
                    carbon_limit_ratio = c.details["grid_limit_ratio"]
                else:
                    diesel_prohibited = True

        best_cost = float('inf')
        best_decision = None

        discharge_steps = int(max_discharge / DISCHARGE_STEP_KW) + 1
        if max_discharge > 0 and discharge_steps == 1:
            discharge_steps = 2

        for step in range(discharge_steps):
            bess_discharge_kw = min(step * DISCHARGE_STEP_KW, max_discharge)

            remaining_load = max(0.0, load_kw - total_renewable - bess_discharge_kw)

            if remaining_load <= 0:
                grid_import_kw = 0.0
                diesel_kw = 0.0
                load_shed_kw = 0.0
                grid_export_kw = abs(remaining_load)

                cost, breakdown = self._calculate_cost(
                    grid_import_kw, bess_discharge_kw, diesel_kw,
                    load_shed_kw, grid_export_kw, input_snap, time_interval
                )

                if cost < best_cost:
                    best_cost = cost
                    best_decision = TheoreticalOptimalDecision(
                        bess_discharge_kw=round(bess_discharge_kw, 2),
                        grid_import_kw=round(grid_import_kw, 2),
                        load_served_kw=round(load_kw, 2),
                        load_shed_kw=round(load_shed_kw, 2),
                        total_cost=round(cost, 4),
                        diesel_output_kw=round(diesel_kw, 2),
                        grid_export_kw=round(grid_export_kw, 2),
                        cost_breakdown=breakdown,
                        constraints_applied=constraints,
                    )
                continue

            if carbon_limit_ratio is not None:
                grid_import_max = remaining_load * carbon_limit_ratio
                remaining_after_grid = remaining_load - grid_import_max
                load_shed_kw = remaining_after_grid
                diesel_kw = 0.0
                grid_import_kw = grid_import_max

                cost, breakdown = self._calculate_cost(
                    grid_import_kw, bess_discharge_kw, diesel_kw,
                    load_shed_kw, 0.0, input_snap, time_interval
                )

                if cost < best_cost:
                    best_cost = cost
                    best_decision = TheoreticalOptimalDecision(
                        bess_discharge_kw=round(bess_discharge_kw, 2),
                        grid_import_kw=round(grid_import_kw, 2),
                        load_served_kw=round(load_kw - load_shed_kw, 2),
                        load_shed_kw=round(load_shed_kw, 2),
                        total_cost=round(cost, 4),
                        diesel_output_kw=round(diesel_kw, 2),
                        grid_export_kw=0.0,
                        cost_breakdown=breakdown,
                        constraints_applied=constraints,
                    )
                continue

            use_grid = grid_buy_price < diesel_gen_cost and not diesel_prohibited

            if use_grid or diesel_prohibited:
                grid_import_kw = remaining_load
                diesel_kw = 0.0
                load_shed_kw = 0.0

                cost, breakdown = self._calculate_cost(
                    grid_import_kw, bess_discharge_kw, diesel_kw,
                    load_shed_kw, 0.0, input_snap, time_interval
                )

                if cost < best_cost:
                    best_cost = cost
                    best_decision = TheoreticalOptimalDecision(
                        bess_discharge_kw=round(bess_discharge_kw, 2),
                        grid_import_kw=round(grid_import_kw, 2),
                        load_served_kw=round(load_kw, 2),
                        load_shed_kw=round(load_shed_kw, 2),
                        total_cost=round(cost, 4),
                        diesel_output_kw=round(diesel_kw, 2),
                        grid_export_kw=0.0,
                        cost_breakdown=breakdown,
                        constraints_applied=constraints,
                    )

            if not diesel_prohibited and grid_buy_price >= diesel_gen_cost:
                diesel_available = all(input_snap.diesel_available.values())
                if diesel_available:
                    diesel_capacity = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["rated_power"]
                    diesel_kw = min(remaining_load, diesel_capacity)
                    grid_import_kw = 0.0
                    load_shed_kw = max(0.0, remaining_load - diesel_kw)

                    cost, breakdown = self._calculate_cost(
                        grid_import_kw, bess_discharge_kw, diesel_kw,
                        load_shed_kw, 0.0, input_snap, time_interval
                    )

                    if cost < best_cost:
                        best_cost = cost
                        best_decision = TheoreticalOptimalDecision(
                            bess_discharge_kw=round(bess_discharge_kw, 2),
                            grid_import_kw=round(grid_import_kw, 2),
                            load_served_kw=round(load_kw - load_shed_kw, 2),
                            load_shed_kw=round(load_shed_kw, 2),
                            total_cost=round(cost, 4),
                            diesel_output_kw=round(diesel_kw, 2),
                            grid_export_kw=0.0,
                            cost_breakdown=breakdown,
                            constraints_applied=constraints,
                        )

        if best_decision is None:
            grid_import_kw = max(0.0, load_kw - total_renewable)
            load_shed_kw = 0.0
            cost, breakdown = self._calculate_cost(
                grid_import_kw, 0.0, 0.0, load_shed_kw, 0.0, input_snap, time_interval
            )
            best_decision = TheoreticalOptimalDecision(
                bess_discharge_kw=0.0,
                grid_import_kw=round(grid_import_kw, 2),
                load_served_kw=round(load_kw, 2),
                load_shed_kw=0.0,
                total_cost=round(cost, 4),
                cost_breakdown=breakdown,
                constraints_applied=constraints,
            )

        return best_decision

    def _calculate_quality_score(self, actual_cost: float, theoretical_cost: float) -> Tuple[float, str]:
        if actual_cost <= 0:
            if theoretical_cost <= 0:
                return 100.0, "无成本支出，决策质量优秀"
            return 0.0, "实际成本异常，无法计算质量分"

        score = (theoretical_cost / actual_cost) * 100.0
        score = min(100.0, max(0.0, score))

        if score >= 90:
            explanation = f"决策质量优秀（{score:.1f}分），实际决策非常接近理论最优"
        elif score >= 70:
            explanation = f"决策质量良好（{score:.1f}分），实际决策与理论最优有一定差距"
        elif score >= 60:
            explanation = f"决策质量一般（{score:.1f}分），实际决策与理论最优差距较大"
        else:
            explanation = f"决策质量较差（{score:.1f}分），实际决策显著偏离理论最优，建议检查调度策略"

        return score, explanation

    def replay_single(self, audit_id: str) -> Optional[DecisionQualityResult]:
        audit = self.state.get_audit_log(audit_id)
        if audit is None:
            return None

        return self._replay_audit(audit)

    def _replay_audit(self, audit: AuditLog) -> DecisionQualityResult:
        constraints = self._extract_constraints_from_audit(audit)
        theoretical_optimal = self._find_theoretical_optimal(audit.input_snapshot, constraints)

        actual_cost = audit.output_summary.total_cost
        theoretical_cost = theoretical_optimal.total_cost

        quality_score, explanation = self._calculate_quality_score(actual_cost, theoretical_cost)
        is_low_score = quality_score < LOW_SCORE_THRESHOLD

        if quality_score < CONSECUTIVE_LOW_SCORE_THRESHOLD:
            self._consecutive_low_score_count += 1
        else:
            self._consecutive_low_score_count = 0

        result = DecisionQualityResult(
            replay_id=self._generate_replay_id(),
            audit_id=audit.audit_id,
            dispatch_id=audit.dispatch_id,
            timestamp=audit.timestamp,
            quality_score=round(quality_score, 2),
            is_low_score=is_low_score,
            actual_cost=round(actual_cost, 4),
            theoretical_optimal_cost=round(theoretical_cost, 4),
            cost_difference=round(actual_cost - theoretical_cost, 4),
            cost_savings_potential=round(actual_cost - theoretical_cost, 4),
            theoretical_optimal=theoretical_optimal,
            actual_summary=audit.output_summary,
            constraints_applied=constraints,
            explanation=explanation,
        )

        self._replay_results.append(result)
        return result

    def replay_batch(self,
                     start_time: datetime,
                     end_time: datetime) -> BatchReplayResult:
        audits = self.state.query_audit_logs(
            start_time=start_time,
            end_time=end_time,
            limit=1000000,
            offset=0,
        )

        audits = sorted(audits, key=lambda a: a.timestamp)

        individual_results = []
        scores = []
        low_score_count = 0

        self._consecutive_low_score_count = 0
        max_consecutive_low = 0
        current_consecutive = 0

        for audit in audits:
            result = self._replay_audit(audit)
            individual_results.append(result)
            scores.append(result.quality_score)

            if result.is_low_score:
                low_score_count += 1

            if result.quality_score < CONSECUTIVE_LOW_SCORE_THRESHOLD:
                current_consecutive += 1
                max_consecutive_low = max(max_consecutive_low, current_consecutive)
            else:
                current_consecutive = 0

        if not scores:
            return BatchReplayResult(
                start_time=start_time,
                end_time=end_time,
                total_records=0,
                avg_quality_score=0.0,
                min_quality_score=0.0,
                max_quality_score=0.0,
                low_score_count=0,
                low_score_ratio=0.0,
                consecutive_low_score_warning=False,
                consecutive_low_score_count=0,
                individual_results=[],
                alert_generated=False,
                alert_message="指定时间范围内没有审计日志记录",
            )

        avg_score = sum(scores) / len(scores)
        min_score = min(scores)
        max_score = max(scores)
        low_ratio = low_score_count / len(scores)

        consecutive_warning = max_consecutive_low >= CONSECUTIVE_LOW_SCORE_ROUNDS
        alert_generated = False
        alert_message = ""

        if consecutive_warning:
            alert_generated = True
            alert_message = (
                f"警告：连续{max_consecutive_low}轮调度质量分低于{CONSECUTIVE_LOW_SCORE_THRESHOLD}分，"
                f"平均质量分{avg_score:.1f}分，建议检查并调优调度策略"
            )
            self.state.add_alert(
                "DECISION_QUALITY_WARNING",
                alert_message,
                {
                    "consecutive_count": max_consecutive_low,
                    "avg_score": avg_score,
                    "threshold": CONSECUTIVE_LOW_SCORE_THRESHOLD,
                    "time_range": {
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat(),
                    },
                }
            )

        return BatchReplayResult(
            start_time=start_time,
            end_time=end_time,
            total_records=len(audits),
            avg_quality_score=round(avg_score, 2),
            min_quality_score=round(min_score, 2),
            max_quality_score=round(max_score, 2),
            low_score_count=low_score_count,
            low_score_ratio=round(low_ratio, 4),
            consecutive_low_score_warning=consecutive_warning,
            consecutive_low_score_count=max_consecutive_low,
            individual_results=individual_results,
            alert_generated=alert_generated,
            alert_message=alert_message,
        )

    def get_quality_trend(self,
                          start_time: Optional[datetime] = None,
                          end_time: Optional[datetime] = None,
                          granularity: str = "hour") -> List[QualityScoreTrendPoint]:
        if granularity not in ("hour", "day"):
            raise ValueError("granularity must be 'hour' or 'day'")

        results = self._replay_results
        if start_time:
            results = [r for r in results if r.timestamp >= start_time]
        if end_time:
            results = [r for r in results if r.timestamp <= end_time]

        buckets: Dict[str, List[float]] = {}
        for r in results:
            if granularity == "hour":
                key = r.timestamp.strftime("%Y-%m-%d %H:00")
            else:
                key = r.timestamp.strftime("%Y-%m-%d")

            if key not in buckets:
                buckets[key] = []
            buckets[key].append(r.quality_score)

        trend = []
        for key in sorted(buckets.keys()):
            scores = buckets[key]
            avg_score = sum(scores) / len(scores)
            low_count = sum(1 for s in scores if s < LOW_SCORE_THRESHOLD)
            trend.append(QualityScoreTrendPoint(
                time_key=key,
                avg_quality_score=round(avg_score, 2),
                dispatch_count=len(scores),
                min_score=round(min(scores), 2),
                max_score=round(max(scores), 2),
                low_score_count=low_count,
            ))

        return trend

    def get_low_score_records(self,
                              threshold: Optional[float] = None,
                              start_time: Optional[datetime] = None,
                              end_time: Optional[datetime] = None,
                              limit: int = 100) -> List[LowScoreRecord]:
        if threshold is None:
            threshold = LOW_SCORE_THRESHOLD

        results = self._replay_results
        if start_time:
            results = [r for r in results if r.timestamp >= start_time]
        if end_time:
            results = [r for r in results if r.timestamp <= end_time]

        low_scores = [r for r in results if r.quality_score < threshold]
        low_scores = sorted(low_scores, key=lambda r: r.quality_score)
        low_scores = low_scores[:limit]

        records = []
        for r in low_scores:
            actual = r.actual_summary
            optimal = r.theoretical_optimal

            actual_vs_optimal = {
                "bess_discharge": {
                    "actual": actual.bess_discharge_kw,
                    "optimal": optimal.bess_discharge_kw,
                    "difference": round(actual.bess_discharge_kw - optimal.bess_discharge_kw, 2),
                },
                "grid_import": {
                    "actual": actual.grid_import_kw,
                    "optimal": optimal.grid_import_kw,
                    "difference": round(actual.grid_import_kw - optimal.grid_import_kw, 2),
                },
                "diesel_output": {
                    "actual": actual.diesel_share_kw,
                    "optimal": optimal.diesel_output_kw,
                    "difference": round(actual.diesel_share_kw - optimal.diesel_output_kw, 2),
                },
                "grid_export": {
                    "actual": actual.grid_export_kw,
                    "optimal": optimal.grid_export_kw,
                    "difference": round(actual.grid_export_kw - optimal.grid_export_kw, 2),
                },
                "load_shed": {
                    "actual": actual.load_shed_kw,
                    "optimal": optimal.load_shed_kw,
                    "difference": round(actual.load_shed_kw - optimal.load_shed_kw, 2),
                },
                "cost": {
                    "actual": actual.total_cost,
                    "optimal": optimal.total_cost,
                    "difference": round(actual.total_cost - optimal.total_cost, 4),
                },
            }

            records.append(LowScoreRecord(
                quality_result=r,
                actual_vs_optimal=actual_vs_optimal,
            ))

        return records

    def get_replay_result(self, replay_id: str) -> Optional[DecisionQualityResult]:
        for r in self._replay_results:
            if r.replay_id == replay_id:
                return r
        return None

    def clear_replay_results(self):
        self._replay_results = []
        self._replay_counter = 0
        self._consecutive_low_score_count = 0
