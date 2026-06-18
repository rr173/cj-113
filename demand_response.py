from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from copy import deepcopy
import uuid
import config


@dataclass
class InterruptibleLoad:
    load_id: str
    name: str
    rated_power_kw: float
    max_reduction_ratio: float
    min_duration_minutes: int
    cooldown_minutes: int
    unit_cost_yuan_per_kwh: float
    created_at: datetime = field(default_factory=datetime.now)
    last_reduction_end_time: Optional[datetime] = None
    current_reduction_kw: float = 0.0

    def get_max_reduction_kw(self) -> float:
        return self.rated_power_kw * self.max_reduction_ratio

    def can_reduce(self, now: datetime) -> bool:
        if self.last_reduction_end_time is None:
            return True
        cooldown_end = self.last_reduction_end_time + timedelta(minutes=self.cooldown_minutes)
        return now >= cooldown_end

    def get_available_reduction_kw(self, now: datetime) -> float:
        if not self.can_reduce(now):
            return 0.0
        return self.get_max_reduction_kw()


@dataclass
class SchedulePeriodReduction:
    period_start: datetime
    period_end: datetime
    load_reductions: Dict[str, float] = field(default_factory=dict)
    battery_discharge_kw: float = 0.0
    total_reduction_kw: float = 0.0
    target_load_kw: float = 0.0
    estimated_load_kw: float = 0.0


@dataclass
class ResponsePlan:
    plan_id: str
    event_id: str
    generated_at: datetime
    status: str = "pending"
    schedule: List[SchedulePeriodReduction] = field(default_factory=list)
    total_reduction_target_kw: float = 0.0
    is_partial_response: bool = False
    expected_gap_kw: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class ExecutionRecord:
    record_id: str
    event_id: str
    timestamp: datetime
    actual_load_kw: float
    target_load_kw: float
    total_reduction_kw: float
    load_reductions: Dict[str, float] = field(default_factory=dict)
    battery_discharge_kw: float = 0.0
    is_compliant: bool = True
    gap_kw: float = 0.0


@dataclass
class SettlementReport:
    report_id: str
    event_id: str
    generated_at: datetime
    start_time: datetime
    end_time: datetime
    total_periods: int = 0
    compliant_periods: int = 0
    compliance_rate: float = 0.0
    total_reduction_kwh: float = 0.0
    total_gap_kwh: float = 0.0
    subsidy_unit_price: float = 0.0
    penalty_unit_price: float = 0.0
    subsidy_amount: float = 0.0
    penalty_amount: float = 0.0
    net_amount: float = 0.0
    settlement_type: str = "full"


@dataclass
class DemandResponseEvent:
    event_id: str
    event_no: str
    start_time: datetime
    end_time: datetime
    target_load_kw: float
    subsidy_unit_price: float
    penalty_unit_price: float
    status: str = "pending"
    received_at: datetime = field(default_factory=datetime.now)
    confirmed_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    current_plan_id: Optional[str] = None
    settlement_report_id: Optional[str] = None
    early_terminated: bool = False
    termination_reason: str = ""


class DemandResponseManager:
    def __init__(self, state):
        self.state = state
        self.interruptible_loads: Dict[str, InterruptibleLoad] = {}
        self.events: Dict[str, DemandResponseEvent] = {}
        self.plans: Dict[str, ResponsePlan] = {}
        self.execution_records: Dict[str, List[ExecutionRecord]] = {}
        self.settlement_reports: Dict[str, SettlementReport] = {}
        self._init_default_loads()

    def _init_default_loads(self):
        default_loads = [
            {
                "load_id": "ac_main",
                "name": "主空调系统",
                "rated_power_kw": 80.0,
                "max_reduction_ratio": 0.5,
                "min_duration_minutes": 30,
                "cooldown_minutes": 15,
                "unit_cost_yuan_per_kwh": 0.3,
            },
            {
                "load_id": "lighting_area_a",
                "name": "A区照明回路",
                "rated_power_kw": 30.0,
                "max_reduction_ratio": 0.7,
                "min_duration_minutes": 15,
                "cooldown_minutes": 5,
                "unit_cost_yuan_per_kwh": 0.2,
            },
            {
                "load_id": "production_line_1",
                "name": "1号生产线",
                "rated_power_kw": 150.0,
                "max_reduction_ratio": 0.3,
                "min_duration_minutes": 60,
                "cooldown_minutes": 30,
                "unit_cost_yuan_per_kwh": 1.5,
            },
            {
                "load_id": "pumping_system",
                "name": "水泵系统",
                "rated_power_kw": 45.0,
                "max_reduction_ratio": 0.4,
                "min_duration_minutes": 20,
                "cooldown_minutes": 10,
                "unit_cost_yuan_per_kwh": 0.5,
            },
        ]
        for load_cfg in default_loads:
            load = InterruptibleLoad(**load_cfg)
            self.interruptible_loads[load.load_id] = load

    def add_interruptible_load(self, load_config: Dict[str, Any]) -> InterruptibleLoad:
        load_id = load_config.get("load_id")
        if not load_id:
            load_id = f"load_{uuid.uuid4().hex[:8]}"
        if load_id in self.interruptible_loads:
            raise ValueError(f"可中断负荷ID已存在: {load_id}")

        load = InterruptibleLoad(
            load_id=load_id,
            name=load_config["name"],
            rated_power_kw=float(load_config["rated_power_kw"]),
            max_reduction_ratio=float(load_config["max_reduction_ratio"]),
            min_duration_minutes=int(load_config["min_duration_minutes"]),
            cooldown_minutes=int(load_config["cooldown_minutes"]),
            unit_cost_yuan_per_kwh=float(load_config["unit_cost_yuan_per_kwh"]),
        )
        self.interruptible_loads[load_id] = load
        return load

    def update_interruptible_load(self, load_id: str, updates: Dict[str, Any]) -> Optional[InterruptibleLoad]:
        if load_id not in self.interruptible_loads:
            return None
        load = self.interruptible_loads[load_id]
        if "name" in updates:
            load.name = updates["name"]
        if "rated_power_kw" in updates:
            load.rated_power_kw = float(updates["rated_power_kw"])
        if "max_reduction_ratio" in updates:
            load.max_reduction_ratio = float(updates["max_reduction_ratio"])
        if "min_duration_minutes" in updates:
            load.min_duration_minutes = int(updates["min_duration_minutes"])
        if "cooldown_minutes" in updates:
            load.cooldown_minutes = int(updates["cooldown_minutes"])
        if "unit_cost_yuan_per_kwh" in updates:
            load.unit_cost_yuan_per_kwh = float(updates["unit_cost_yuan_per_kwh"])
        return load

    def delete_interruptible_load(self, load_id: str) -> bool:
        if load_id not in self.interruptible_loads:
            return False
        del self.interruptible_loads[load_id]
        return True

    def get_interruptible_load(self, load_id: str) -> Optional[InterruptibleLoad]:
        return self.interruptible_loads.get(load_id)

    def list_interruptible_loads(self) -> List[InterruptibleLoad]:
        return list(self.interruptible_loads.values())

    def receive_event(self, event_data: Dict[str, Any]) -> DemandResponseEvent:
        event_id = f"dr_{uuid.uuid4().hex[:12]}"
        event = DemandResponseEvent(
            event_id=event_id,
            event_no=event_data.get("event_no", event_id),
            start_time=event_data["start_time"] if isinstance(event_data["start_time"], datetime) else datetime.fromisoformat(event_data["start_time"]),
            end_time=event_data["end_time"] if isinstance(event_data["end_time"], datetime) else datetime.fromisoformat(event_data["end_time"]),
            target_load_kw=float(event_data["target_load_kw"]),
            subsidy_unit_price=float(event_data["subsidy_unit_price"]),
            penalty_unit_price=float(event_data["penalty_unit_price"]),
        )
        self.events[event_id] = event
        self.execution_records[event_id] = []
        return event

    def get_current_load_kw(self) -> float:
        return self.state.get_load_kw() if self.state.load_report else 0.0

    def generate_response_plan(self, event_id: str) -> Optional[ResponsePlan]:
        event = self.events.get(event_id)
        if not event:
            return None

        now = datetime.now()
        current_load = self.get_current_load_kw()
        target_load = event.target_load_kw
        gap = max(0.0, current_load - target_load)

        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        plan = ResponsePlan(
            plan_id=plan_id,
            event_id=event_id,
            generated_at=now,
            total_reduction_target_kw=gap,
        )

        if gap <= 0:
            plan.notes.append(f"当前负荷({current_load:.2f}kW)已低于目标值({target_load:.2f}kW)，无需削减")
            self.plans[plan_id] = plan
            event.current_plan_id = plan_id
            return plan

        available_loads = []
        for load in self.interruptible_loads.values():
            avail = load.get_available_reduction_kw(now)
            if avail > 0:
                available_loads.append({
                    "load": load,
                    "available_kw": avail,
                    "unit_cost": load.unit_cost_yuan_per_kwh,
                })

        available_loads.sort(key=lambda x: x["unit_cost"])

        remaining_gap = gap
        load_reductions = {}

        for item in available_loads:
            if remaining_gap <= 0:
                break
            load = item["load"]
            reduce_kw = min(item["available_kw"], remaining_gap)
            load_reductions[load.load_id] = reduce_kw
            remaining_gap -= reduce_kw
            plan.notes.append(
                f"安排负荷[{load.name}]削减{reduce_kw:.2f}kW "
                f"(单位成本{load.unit_cost_yuan_per_kwh:.2f}元/kWh)"
            )

        battery_discharge = 0.0
        if remaining_gap > 0:
            bes_id = list(config.BESS_CONFIG.keys())[0]
            time_interval_hours = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0
            max_discharge = self.state.get_bess_max_discharge_with_health(bes_id, time_interval_hours)
            battery_discharge = min(max_discharge, remaining_gap)
            if battery_discharge > 0:
                remaining_gap -= battery_discharge
                plan.notes.append(f"安排电池放电{battery_discharge:.2f}kW补充")

        if remaining_gap > 0:
            plan.is_partial_response = True
            plan.expected_gap_kw = remaining_gap
            plan.notes.append(f"警告：全部可调节资源用尽仍有{remaining_gap:.2f}kW缺口，方案为部分响应")

        start = event.start_time
        end = event.end_time
        interval = timedelta(minutes=config.DEFAULT_DISPATCH_INTERVAL_MINUTES)

        current_period_start = start
        while current_period_start < end:
            period_end = min(current_period_start + interval, end)
            period_reduction = SchedulePeriodReduction(
                period_start=current_period_start,
                period_end=period_end,
                load_reductions=dict(load_reductions),
                battery_discharge_kw=battery_discharge,
                total_reduction_kw=gap - remaining_gap,
                target_load_kw=target_load,
                estimated_load_kw=current_load - (gap - remaining_gap),
            )
            plan.schedule.append(period_reduction)
            current_period_start = period_end

        self.plans[plan_id] = plan
        event.current_plan_id = plan_id
        return plan

    def confirm_plan(self, event_id: str) -> bool:
        event = self.events.get(event_id)
        if not event or event.status != "pending":
            return False
        if not event.current_plan_id:
            return False

        event.status = "confirmed"
        event.confirmed_at = datetime.now()
        return True

    def start_event_if_due(self, now: datetime = None) -> List[str]:
        if now is None:
            now = datetime.now()

        started_events = []
        for event in self.events.values():
            if event.status == "confirmed" and event.start_time <= now <= event.end_time:
                event.status = "active"
                started_events.append(event.event_id)
        return started_events

    def get_active_event(self, now: datetime = None) -> Optional[DemandResponseEvent]:
        if now is None:
            now = datetime.now()
        for event in self.events.values():
            if event.status == "active" and event.start_time <= now <= event.end_time:
                return event
        for event in self.events.values():
            if event.status == "active":
                return None
        return None

    def get_current_reduction(self, now: datetime = None) -> Dict[str, Any]:
        if now is None:
            now = datetime.now()

        active_event = self.get_active_event(now)
        if not active_event or not active_event.current_plan_id:
            return {
                "active": False,
                "load_reductions": {},
                "battery_discharge_kw": 0.0,
                "target_load_kw": 0.0,
                "debug_reason": "no_active_event",
            }

        plan = self.plans.get(active_event.current_plan_id)
        if not plan:
            return {
                "active": False,
                "load_reductions": {},
                "battery_discharge_kw": 0.0,
                "target_load_kw": 0.0,
                "debug_reason": "plan_not_found",
            }
        if not plan.schedule:
            return {
                "active": False,
                "load_reductions": {},
                "battery_discharge_kw": 0.0,
                "target_load_kw": 0.0,
                "debug_reason": "plan_schedule_empty",
            }

        current_period = None
        for period in plan.schedule:
            if period.period_start <= now < period.period_end:
                current_period = period
                break

        if current_period is None:
            if now < plan.schedule[0].period_start:
                current_period = plan.schedule[0]
                debug_reason = "used_first_period"
            elif now >= plan.schedule[-1].period_end:
                current_period = plan.schedule[-1]
                debug_reason = "used_last_period"
            else:
                min_diff = None
                for period in plan.schedule:
                    diff = abs((period.period_start - now).total_seconds())
                    if min_diff is None or diff < min_diff:
                        min_diff = diff
                        current_period = period
                debug_reason = "used_nearest_period"
        else:
            debug_reason = "exact_match"

        return {
            "active": True,
            "event_id": active_event.event_id,
            "target_load_kw": active_event.target_load_kw,
            "load_reductions": current_period.load_reductions,
            "battery_discharge_kw": current_period.battery_discharge_kw,
            "debug_reason": debug_reason,
        }

    def record_execution(self, event_id: str, actual_load_kw: float,
                         load_reductions: Dict[str, float], battery_discharge_kw: float,
                         timestamp: datetime = None) -> Optional[ExecutionRecord]:
        event = self.events.get(event_id)
        if not event:
            return None

        if timestamp is None:
            timestamp = datetime.now()

        total_reduction = sum(load_reductions.values()) + battery_discharge_kw
        is_compliant = actual_load_kw <= event.target_load_kw
        gap = max(0.0, actual_load_kw - event.target_load_kw)

        record = ExecutionRecord(
            record_id=f"rec_{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            timestamp=timestamp,
            actual_load_kw=actual_load_kw,
            target_load_kw=event.target_load_kw,
            total_reduction_kw=total_reduction,
            load_reductions=dict(load_reductions),
            battery_discharge_kw=battery_discharge_kw,
            is_compliant=is_compliant,
            gap_kw=gap,
        )

        if event_id not in self.execution_records:
            self.execution_records[event_id] = []
        self.execution_records[event_id].append(record)

        return record

    def check_and_finish_events(self, now: datetime = None) -> List[str]:
        if now is None:
            now = datetime.now()

        finished_events = []
        for event in self.events.values():
            if event.status == "active" and now > event.end_time:
                event.status = "finished"
                event.finished_at = now
                self.generate_settlement_report(event.event_id)
                finished_events.append(event.event_id)

        return finished_events

    def terminate_event_early(self, event_id: str, reason: str = "手动中止") -> Optional[SettlementReport]:
        event = self.events.get(event_id)
        if not event or event.status != "active":
            return None

        now = datetime.now()
        event.status = "finished"
        event.finished_at = now
        event.early_terminated = True
        event.termination_reason = reason

        return self.generate_settlement_report(event_id)

    def generate_settlement_report(self, event_id: str) -> Optional[SettlementReport]:
        event = self.events.get(event_id)
        if not event:
            return None

        records = self.execution_records.get(event_id, [])
        if not records:
            return None

        total_periods = len(records)
        compliant_periods = sum(1 for r in records if r.is_compliant)
        compliance_rate = compliant_periods / total_periods if total_periods > 0 else 0.0

        interval_minutes = config.DEFAULT_DISPATCH_INTERVAL_MINUTES
        interval_hours = interval_minutes / 60.0

        total_reduction_kwh = sum(r.total_reduction_kw * interval_hours for r in records)
        total_gap_kwh = sum(r.gap_kw * interval_hours for r in records)

        subsidy_amount = 0.0
        penalty_amount = 0.0
        settlement_type = "none"

        if compliance_rate >= 1.0:
            settlement_type = "full"
            subsidy_amount = total_reduction_kwh * event.subsidy_unit_price
        elif compliance_rate >= 0.9:
            settlement_type = "partial"
            subsidy_amount = total_reduction_kwh * event.subsidy_unit_price * 0.8
        else:
            settlement_type = "penalty"
            penalty_amount = total_gap_kwh * event.penalty_unit_price

        net_amount = subsidy_amount - penalty_amount

        report = SettlementReport(
            report_id=f"settle_{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            generated_at=datetime.now(),
            start_time=event.start_time,
            end_time=event.finished_at or event.end_time,
            total_periods=total_periods,
            compliant_periods=compliant_periods,
            compliance_rate=compliance_rate,
            total_reduction_kwh=total_reduction_kwh,
            total_gap_kwh=total_gap_kwh,
            subsidy_unit_price=event.subsidy_unit_price,
            penalty_unit_price=event.penalty_unit_price,
            subsidy_amount=subsidy_amount,
            penalty_amount=penalty_amount,
            net_amount=net_amount,
            settlement_type=settlement_type,
        )

        self.settlement_reports[event_id] = report
        event.settlement_report_id = report.report_id

        return report

    def list_events(self, status: str = None) -> List[DemandResponseEvent]:
        events = list(self.events.values())
        if status:
            events = [e for e in events if e.status == status]
        events.sort(key=lambda e: e.received_at, reverse=True)
        return events

    def get_event(self, event_id: str) -> Optional[DemandResponseEvent]:
        return self.events.get(event_id)

    def get_plan(self, plan_id: str) -> Optional[ResponsePlan]:
        return self.plans.get(plan_id)

    def get_event_execution_records(self, event_id: str) -> List[ExecutionRecord]:
        return self.execution_records.get(event_id, [])

    def get_settlement_report(self, event_id: str) -> Optional[SettlementReport]:
        return self.settlement_reports.get(event_id)

    def get_accumulated_stats(self) -> Dict[str, Any]:
        total_events = len(self.events)
        finished_events = [e for e in self.events.values() if e.status == "finished"]
        total_subsidy = 0.0
        total_penalty = 0.0
        total_reduction_kwh = 0.0

        for event in finished_events:
            report = self.settlement_reports.get(event.event_id)
            if report:
                total_subsidy += report.subsidy_amount
                total_penalty += report.penalty_amount
                total_reduction_kwh += report.total_reduction_kwh

        return {
            "total_events": total_events,
            "finished_events": len(finished_events),
            "active_events": len([e for e in self.events.values() if e.status == "active"]),
            "pending_events": len([e for e in self.events.values() if e.status in ["pending", "confirmed"]]),
            "total_subsidy": round(total_subsidy, 2),
            "total_penalty": round(total_penalty, 2),
            "net_income": round(total_subsidy - total_penalty, 2),
            "total_reduction_kwh": round(total_reduction_kwh, 2),
        }

    def apply_dr_constraints(self, decision, now: datetime = None):
        if now is None:
            now = datetime.now()

        active_events = [e for e in self.events.values() if e.status == "active"]
        if not active_events:
            return decision

        if len(active_events) > 0:
            decision.notes.append(
                f"[需求响应-调试] 检测到{len(active_events)}个active状态事件"
            )
            for e in active_events:
                decision.notes.append(
                    f"[需求响应-调试]   事件{e.event_id}: 时间[{e.start_time}~{e.end_time}], "
                    f"目标{e.target_load_kw}kW, now={now}, "
                    f"in_time_window={e.start_time <= now <= e.end_time}"
                )

        active_event = self.get_active_event(now)
        if not active_event:
            decision.notes.append("[需求响应-调试] 无事件满足时间窗口约束，跳过削减")
            return decision

        decision.notes.append(
            f"[需求响应] 事件{active_event.event_id}生效中 "
            f"(时间窗口: {active_event.start_time.strftime('%H:%M')}~{active_event.end_time.strftime('%H:%M')}, "
            f"目标: {active_event.target_load_kw}kW)"
        )

        dr_info = self.get_current_reduction(now)
        debug_reason = dr_info.get("debug_reason", "unknown")

        if not dr_info.get("active"):
            decision.notes.append(
                f"[需求响应-调试] 方案不活跃 (原因: {debug_reason}), "
                f"plan_id={active_event.current_plan_id}"
            )
            return decision

        decision.notes.append(f"[需求响应-调试] 方案时段匹配模式: {debug_reason}")

        target_load = dr_info["target_load_kw"]
        load_reductions = dr_info.get("load_reductions", {})
        total_reduction = sum(load_reductions.values())
        battery_extra_discharge = dr_info.get("battery_discharge_kw", 0.0)

        current_load = decision.load_served_kw

        decision.notes.append(
            f"[需求响应] 当前负荷{current_load:.2f}kW, "
            f"需削减至{target_load:.2f}kW, "
            f"计划削减{total_reduction:.2f}kW"
            + (f"+电池{battery_extra_discharge:.2f}kW" if battery_extra_discharge > 0 else "")
        )

        reduction_details = []
        for load_id, kw in load_reductions.items():
            load = self.interruptible_loads.get(load_id)
            name = load.name if load else load_id
            reduction_details.append(f"{name} {kw:.1f}kW")
        if reduction_details:
            decision.notes.append(f"[需求响应] 削减明细: {', '.join(reduction_details)}")

        if current_load <= target_load:
            decision.notes.append(
                f"[需求响应] 当前负荷{current_load:.2f}kW已低于目标{target_load:.2f}kW，满足约束"
            )
            self.record_execution(
                active_event.event_id,
                current_load,
                load_reductions,
                battery_extra_discharge,
                now,
            )
            return decision

        load_after_reduction = current_load - total_reduction

        if load_after_reduction <= target_load:
            actual_reduction = current_load - target_load
            decision.notes.append(
                f"[需求响应] 削减负荷{actual_reduction:.2f}kW，"
                f"从{current_load:.2f}kW降至{target_load:.2f}kW，满足目标"
            )
            decision.load_served_kw = target_load
            decision.load_shed_kw += actual_reduction

            applied_ratio = actual_reduction / total_reduction if total_reduction > 0 else 1.0
            applied_reductions = {
                lid: kw * applied_ratio for lid, kw in load_reductions.items()
            }

            for load_id, reduction_kw in applied_reductions.items():
                load = self.interruptible_loads.get(load_id)
                if load:
                    load.current_reduction_kw = reduction_kw
                    load.last_reduction_end_time = now

            self.record_execution(
                active_event.event_id,
                target_load,
                applied_reductions,
                0.0,
                now,
            )
            return decision

        load_after_reduction_and_battery = load_after_reduction - battery_extra_discharge

        if load_after_reduction_and_battery <= target_load:
            extra_discharge_needed = load_after_reduction - target_load
            decision.notes.append(
                f"[需求响应] 削减负荷{total_reduction:.2f}kW + 电池追加放电{extra_discharge_needed:.2f}kW，"
                f"满足目标{target_load:.2f}kW"
            )
            decision.load_served_kw = target_load
            decision.load_shed_kw += total_reduction

            bes_id = list(config.BESS_CONFIG.keys())[0]
            current_discharge = decision.bess_action[bes_id].get("discharge_kw", 0.0)
            decision.bess_action[bes_id]["discharge_kw"] = current_discharge + extra_discharge_needed

            for load_id, reduction_kw in load_reductions.items():
                load = self.interruptible_loads.get(load_id)
                if load:
                    load.current_reduction_kw = reduction_kw
                    load.last_reduction_end_time = now

            self.record_execution(
                active_event.event_id,
                target_load,
                load_reductions,
                extra_discharge_needed,
                now,
            )
            return decision

        final_gap = target_load - load_after_reduction_and_battery
        decision.notes.append(
            f"[需求响应] 警告：最大可调节能力不足，"
            f"削减后负荷{load_after_reduction_and_battery:.2f}kW仍高于目标{target_load:.2f}kW，"
            f"缺口{abs(final_gap):.2f}kW"
        )
        decision.load_served_kw = load_after_reduction_and_battery
        decision.load_shed_kw += total_reduction

        bes_id = list(config.BESS_CONFIG.keys())[0]
        current_discharge = decision.bess_action[bes_id].get("discharge_kw", 0.0)
        decision.bess_action[bes_id]["discharge_kw"] = current_discharge + battery_extra_discharge

        for load_id, reduction_kw in load_reductions.items():
            load = self.interruptible_loads.get(load_id)
            if load:
                load.current_reduction_kw = reduction_kw
                load.last_reduction_end_time = now

        self.record_execution(
            active_event.event_id,
            load_after_reduction_and_battery,
            load_reductions,
            battery_extra_discharge,
            now,
        )

        return decision
