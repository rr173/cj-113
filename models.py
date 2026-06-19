from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from copy import deepcopy

import config


@dataclass
class SourceReport:
    source_id: str
    source_type: str
    power_kw: float
    available: bool
    timestamp: datetime


@dataclass
class LoadGroupReport:
    group_id: str
    actual_power_kw: float
    timestamp: datetime


@dataclass
class LoadReport:
    load_kw: float
    timestamp: datetime
    group_reports: Dict[str, LoadGroupReport] = field(default_factory=dict)


@dataclass
class LoadGroupShedEvent:
    event_id: str
    group_id: str
    group_name: str
    shed_power_kw: float
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_minutes: float = 0.0
    reason: str = ""
    dispatch_id: Optional[str] = None


@dataclass
class LoadGroupReliabilitySnapshot:
    timestamp: datetime
    group_id: str
    is_normal: bool
    shed_power_kw: float


@dataclass
class DispatchDecision:
    timestamp: datetime
    pv_output: Dict[str, float]
    wt_output: Dict[str, float]
    diesel_output: Dict[str, float]
    bess_action: Dict[str, Dict[str, Any]]
    grid_import_kw: float
    grid_export_kw: float
    load_served_kw: float
    load_shed_kw: float
    cost: float
    tariff_period: str
    grid_buy_price: float
    notes: List[str] = field(default_factory=list)
    group_shed_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    group_restore_details: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class DieselState:
    running: bool = False
    output_kw: float = 0.0
    last_start_time: Optional[datetime] = None
    last_stop_time: Optional[datetime] = None
    total_starts: int = 0
    total_runtime_seconds: int = 0
    total_generated_kwh: float = 0.0


@dataclass
class DischargeRecord:
    timestamp: datetime
    discharge_power_kw: float
    soc_before: float
    soc_after: float
    soc_drop_rate: float
    duration_hours: float


@dataclass
class SourceHealthRecord:
    timestamp: datetime
    available: bool
    power_kw: float
    health_score: float
    status: str


@dataclass
class SourceHealthState:
    source_id: str
    source_type: str
    health_score: float = 100.0
    status: str = "normal"
    history: List[SourceHealthRecord] = field(default_factory=list)
    in_maintenance: bool = False
    maintenance_started_at: Optional[datetime] = None
    current_fault: Optional[Dict[str, Any]] = None
    consecutive_unavailable: int = 0


@dataclass
class BackupPlan:
    plan_id: str
    source_id: str
    source_type: str
    generated_at: datetime
    can_cover: bool
    gap_kw: float = 0.0
    suggestions: List[str] = field(default_factory=list)
    alternative_sources: Dict[str, Any] = field(default_factory=dict)
    load_kw: float = 0.0
    lost_capacity_kw: float = 0.0


@dataclass
class FaultEvent:
    event_id: str
    source_id: str
    source_type: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_minutes: float = 0.0
    had_plan: bool = False
    plan_id: Optional[str] = None


@dataclass
class BatteryHealth:
    equivalent_cycles: float = 0.0
    last_cycle_soc_peak: Optional[float] = None
    discharge_records: List[DischargeRecord] = field(default_factory=list)
    baseline_soc_drop_rate: Optional[float] = None
    internal_resistance_abnormal: bool = False
    health_percent: float = 100.0
    total_charged_for_cycle: float = 0.0
    total_discharged_for_cycle: float = 0.0


@dataclass
class BessState:
    soc: float
    charge_power_kw: float = 0.0
    discharge_power_kw: float = 0.0
    total_charged_kwh: float = 0.0
    total_discharged_kwh: float = 0.0
    health: BatteryHealth = field(default_factory=BatteryHealth)


@dataclass
class StoragePlanHour:
    hour: int
    mode: str
    tariff_period: str
    target_soc: Optional[float] = None
    active: bool = True
    abnormal: bool = False


@dataclass
class StorageArbitrageStats:
    total_arbitrage_charge_kwh: float = 0.0
    total_arbitrage_discharge_kwh: float = 0.0
    total_arbitrage_cost: float = 0.0
    total_arbitrage_revenue: float = 0.0


@dataclass
class StoragePlan:
    plan_date: str
    generated_at: datetime
    hours: Dict[int, StoragePlanHour] = field(default_factory=dict)


@dataclass
class AccumulatedStats:
    total_pv_generated_kwh: Dict[str, float] = field(default_factory=dict)
    total_wt_generated_kwh: Dict[str, float] = field(default_factory=dict)
    total_diesel_generated_kwh: float = 0.0
    total_grid_import_kwh: float = 0.0
    total_grid_export_kwh: float = 0.0
    total_diesel_starts: int = 0
    total_cost: float = 0.0
    total_load_shed_kwh: float = 0.0
    arbitrage: StorageArbitrageStats = field(default_factory=StorageArbitrageStats)
    load_grid_import_kwh: float = 0.0


@dataclass
class PriceForecastRecord:
    forecast_id: str
    forecast_date: str
    prices: List[float]
    submitted_at: datetime
    status: str = "pending"
    activated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None


@dataclass
class PriceComparisonHour:
    hour: int
    forecast_price: float
    fixed_price: float
    fixed_period: str
    price_diff: float
    price_diff_ratio: float
    is_valley_opportunity: bool
    is_peak_risk: bool


@dataclass
class PriceComparisonResult:
    forecast_id: str
    forecast_date: str
    hours: List[PriceComparisonHour]
    valley_opportunity_hours: List[int]
    peak_risk_hours: List[int]
    total_valley_savings_potential: float
    total_peak_risk_cost: float
    valley_price_threshold: float
    peak_price_threshold: float


@dataclass
class StrategySuggestionHour:
    hour: int
    suggested_action: str
    reason: str
    forecast_price: float
    fixed_price: float


@dataclass
class PurchaseStrategy:
    strategy_id: str
    forecast_id: str
    forecast_date: str
    generated_at: datetime
    status: str
    hours: List[StrategySuggestionHour]
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyExecutionDayStats:
    date: str
    strategy_used: bool
    strategy_id: Optional[str]
    avg_buy_price: float
    total_grid_import_kwh: float
    total_buy_cost: float
    total_load_served_kwh: float
    total_load_shed_kwh: float = 0.0


@dataclass
class StrategyExecutionStatsSummary:
    strategy_days: int
    no_strategy_days: int
    avg_cost_with_strategy: float
    avg_cost_without_strategy: float
    cost_saving_ratio: float
    total_saving: float
    details: List[StrategyExecutionDayStats] = field(default_factory=list)


@dataclass
class InputSnapshot:
    pv_output: Dict[str, float]
    wt_output: Dict[str, float]
    diesel_available: Dict[str, bool]
    load_kw: float
    bess_soc: Dict[str, float]
    grid_buy_price: float
    feed_in_price: float
    tariff_period: str
    hour: int
    storage_strategy_active: bool
    storage_mode: str
    demand_response_active: bool
    active_backup_plans: List[str]
    source_health_status: Dict[str, str]


@dataclass
class DecisionBranch:
    branch_name: str
    decision: bool
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OutputSummary:
    load_served_kw: float
    load_shed_kw: float
    load_coverage_ratio: float
    total_cost: float
    pv_share_kw: float
    wt_share_kw: float
    diesel_share_kw: float
    bess_discharge_kw: float
    grid_import_kw: float
    grid_export_kw: float
    cost_breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class AnomalyMarker:
    anomaly_type: str
    severity: str
    description: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditLog:
    audit_id: str
    dispatch_id: str
    timestamp: datetime
    input_snapshot: InputSnapshot
    decision_branches: List[DecisionBranch]
    output_summary: OutputSummary
    anomalies: List[AnomalyMarker] = field(default_factory=list)
    reasoning_chain: List[str] = field(default_factory=list)

    def has_anomaly(self) -> bool:
        return len(self.anomalies) > 0


class MicrogridState:
    HEALTH_WINDOW_SIZE = 50
    HEALTH_WARNING_THRESHOLD = 60.0
    HEALTH_DANGER_THRESHOLD = 30.0
    FLUCTUATION_STABLE = 0.10
    FLUCTUATION_HIGH = 0.30

    def __init__(self):
        self.pv_reports: Dict[str, SourceReport] = {}
        self.wt_reports: Dict[str, SourceReport] = {}
        self.diesel_reports: Dict[str, SourceReport] = {}
        self.load_report: Optional[LoadReport] = None

        self.diesel_state: Dict[str, DieselState] = {}
        for ds_id in config.DIESEL_CONFIG:
            self.diesel_state[ds_id] = DieselState()

        self.bess_state: Dict[str, BessState] = {}
        for bes_id, cfg in config.BESS_CONFIG.items():
            self.bess_state[bes_id] = BessState(soc=cfg["initial_soc"])

        self.dispatch_history: List[DispatchDecision] = []
        self.stats = AccumulatedStats()

        for pv_id in config.PV_CONFIG:
            self.stats.total_pv_generated_kwh[pv_id] = 0.0
        for wt_id in config.WT_CONFIG:
            self.stats.total_wt_generated_kwh[wt_id] = 0.0

        self.last_dispatch_time: Optional[datetime] = None
        self.alerts: List[Dict[str, Any]] = []

        self.current_storage_plan: Optional[StoragePlan] = None
        self.last_plan_generation_date: Optional[str] = None
        self.plan_generation_hour: int = config.STORAGE_STRATEGY_CONFIG["plan_generation_hour"]
        self.plan_generation_minute: int = config.STORAGE_STRATEGY_CONFIG["plan_generation_minute"]

        self.source_health: Dict[str, SourceHealthState] = {}
        for sid in config.PV_CONFIG:
            self.source_health[f"pv:{sid}"] = SourceHealthState(source_id=sid, source_type="pv")
        for sid in config.WT_CONFIG:
            self.source_health[f"wt:{sid}"] = SourceHealthState(source_id=sid, source_type="wt")
        for sid in config.DIESEL_CONFIG:
            self.source_health[f"diesel:{sid}"] = SourceHealthState(source_id=sid, source_type="diesel")

        self.backup_plans: Dict[str, BackupPlan] = {}
        self.fault_events: List[FaultEvent] = []
        self._fault_event_counter: int = 0
        self._backup_plan_counter: int = 0

        self.audit_logs: List[AuditLog] = []
        self._audit_counter: int = 0
        self._dispatch_counter: int = 0

        self.load_group_reports: Dict[str, LoadGroupReport] = {}
        self.load_group_state: Dict[str, Dict[str, Any]] = {}
        for gid in config.LOAD_GROUP_CONFIG:
            cfg = config.LOAD_GROUP_CONFIG[gid]
            self.load_group_state[gid] = {
                "group_id": gid,
                "name": cfg["name"],
                "description": cfg["description"],
                "rated_power_kw": cfg["rated_power_kw"],
                "max_shed_ratio": cfg["max_shed_ratio"],
                "shed_priority": cfg["shed_priority"],
                "restore_priority": cfg["restore_priority"],
                "reported_power_kw": 0.0,
                "current_served_kw": 0.0,
                "current_shed_kw": 0.0,
                "last_report_time": None,
            }

        self.load_group_shed_events: List[LoadGroupShedEvent] = []
        self._load_group_event_counter: int = 0
        self._active_shed_events: Dict[str, LoadGroupShedEvent] = {}

        self.load_group_reliability_history: Dict[str, List[LoadGroupReliabilitySnapshot]] = {}
        for gid in config.LOAD_GROUP_CONFIG:
            self.load_group_reliability_history[gid] = []

    def report_source(self, report: SourceReport):
        if report.source_type == "pv":
            self.pv_reports[report.source_id] = report
        elif report.source_type == "wt":
            self.wt_reports[report.source_id] = report
        elif report.source_type == "diesel":
            self.diesel_reports[report.source_id] = report

        health_key = f"{report.source_type}:{report.source_id}"
        if health_key in self.source_health:
            self._update_source_health(health_key, report)
            self._handle_fault_transition(health_key, report)
            hs = self.source_health[health_key]
            if not hs.in_maintenance and hs.status in ("warning", "danger"):
                self._generate_backup_plan(health_key)

    def report_load(self, report: LoadReport):
        self.load_report = report
        for gid, gr in report.group_reports.items():
            if gid in self.load_group_state:
                self.load_group_state[gid]["reported_power_kw"] = max(0.0, gr.actual_power_kw)
                self.load_group_state[gid]["last_report_time"] = gr.timestamp
                self.load_group_reports[gid] = gr

    def report_load_group(self, group_id: str, actual_power_kw: float, timestamp: datetime = None):
        if timestamp is None:
            timestamp = datetime.now()
        if group_id not in self.load_group_state:
            raise ValueError(f"未知的负荷群组: {group_id}")

        gr = LoadGroupReport(
            group_id=group_id,
            actual_power_kw=max(0.0, actual_power_kw),
            timestamp=timestamp,
        )
        self.load_group_reports[group_id] = gr
        self.load_group_state[group_id]["reported_power_kw"] = gr.actual_power_kw
        self.load_group_state[group_id]["last_report_time"] = timestamp

        total = self._compute_total_load_from_groups()
        if self.load_report is None:
            self.load_report = LoadReport(
                load_kw=total,
                timestamp=timestamp,
                group_reports={group_id: gr},
            )
        else:
            self.load_report.load_kw = total
            self.load_report.timestamp = timestamp
            self.load_report.group_reports[group_id] = gr

    def _compute_total_load_from_groups(self) -> float:
        total = 0.0
        for gid, gs in self.load_group_state.items():
            total += max(0.0, gs["reported_power_kw"])
        return total

    def all_groups_reported(self) -> bool:
        return all(gid in self.load_group_reports for gid in config.LOAD_GROUP_CONFIG)

    def all_sources_reported(self) -> bool:
        pv_ok = all(sid in self.pv_reports for sid in config.PV_CONFIG)
        wt_ok = all(sid in self.wt_reports for sid in config.WT_CONFIG)
        return pv_ok and wt_ok and self.load_report is not None

    def get_total_renewable_kw(self) -> float:
        total = 0.0
        for r in self.pv_reports.values():
            if r.available:
                total += max(0.0, r.power_kw)
        for r in self.wt_reports.values():
            if r.available:
                total += max(0.0, r.power_kw)
        return total

    def get_load_kw(self) -> float:
        return max(0.0, self.load_report.load_kw) if self.load_report else 0.0

    def get_available_diesel_capacity(self, ds_id: str, now: datetime) -> Dict[str, Any]:
        cfg = config.DIESEL_CONFIG[ds_id]
        ds = self.diesel_state[ds_id]
        report = self.diesel_reports.get(ds_id)

        if report is None or not report.available:
            return {"can_run": False, "max_output": 0.0, "reason": "柴油机不可用或未上报"}

        rated = cfg["rated_power"]

        if ds.running:
            elapsed = (now - ds.last_start_time).total_seconds() / 60.0 if ds.last_start_time else 0
            return {
                "can_run": True,
                "max_output": rated,
                "already_running": True,
                "min_runtime_elapsed": elapsed >= cfg["min_runtime_minutes"],
                "startup_cost_applies": False,
            }
        else:
            if ds.last_stop_time:
                cooldown_passed = (now - ds.last_stop_time).total_seconds() / 60.0 >= cfg["cooldown_minutes"]
            else:
                cooldown_passed = True
            return {
                "can_run": cooldown_passed,
                "max_output": rated if cooldown_passed else 0.0,
                "already_running": False,
                "cooldown_passed": cooldown_passed,
                "startup_cost_applies": cooldown_passed,
            }

    def get_bess_max_discharge(self, bes_id: str, time_interval_hours: float) -> float:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        soc_min = cfg["soc_min"]
        if bs.soc <= soc_min:
            return 0.0
        energy_avail = (bs.soc - soc_min) * cfg["capacity_kwh"]
        max_by_energy = energy_avail * cfg["discharge_efficiency"] / time_interval_hours
        return min(cfg["max_discharge_power"], max_by_energy)

    def get_bess_max_charge(self, bes_id: str, time_interval_hours: float) -> float:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        soc_max = cfg["soc_max"]
        if bs.soc >= soc_max:
            return 0.0
        energy_avail = (soc_max - bs.soc) * cfg["capacity_kwh"]
        max_by_energy = energy_avail / cfg["charge_efficiency"] / time_interval_hours
        return min(cfg["max_charge_power"], max_by_energy)

    def update_bess_soc(self, bes_id: str, charge_kw: float, discharge_kw: float, time_interval_hours: float):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]

        if charge_kw > 0:
            energy_into = charge_kw * time_interval_hours * cfg["charge_efficiency"]
            bs.soc = min(cfg["soc_max"], bs.soc + energy_into / cfg["capacity_kwh"])
            bs.charge_power_kw = charge_kw
            bs.total_charged_kwh += charge_kw * time_interval_hours
        else:
            bs.charge_power_kw = 0.0

        if discharge_kw > 0:
            energy_out = discharge_kw * time_interval_hours / cfg["discharge_efficiency"]
            bs.soc = max(cfg["soc_min"], bs.soc - energy_out / cfg["capacity_kwh"])
            bs.discharge_power_kw = discharge_kw
            bs.total_discharged_kwh += discharge_kw * time_interval_hours
        else:
            bs.discharge_power_kw = 0.0

    def start_diesel(self, ds_id: str, now: datetime):
        ds = self.diesel_state[ds_id]
        if not ds.running:
            ds.running = True
            ds.last_start_time = now
            ds.total_starts += 1
            self.stats.total_diesel_starts += 1

    def stop_diesel(self, ds_id: str, now: datetime):
        ds = self.diesel_state[ds_id]
        if ds.running:
            ds.running = False
            ds.output_kw = 0.0
            if ds.last_start_time:
                ds.total_runtime_seconds += int((now - ds.last_start_time).total_seconds())
            ds.last_stop_time = now

    def add_dispatch(self, decision: DispatchDecision):
        self.dispatch_history.append(decision)
        self.last_dispatch_time = decision.timestamp

    def add_alert(self, alert_type: str, message: str, data: Dict[str, Any] = None):
        self.alerts.append({
            "timestamp": datetime.now().isoformat(),
            "type": alert_type,
            "message": message,
            "data": data or {},
        })
        if len(self.alerts) > 1000:
            self.alerts = self.alerts[-1000:]

    def record_bess_discharge(self, bes_id: str, discharge_kw: float, soc_before: float,
                               soc_after: float, duration_hours: float):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        if discharge_kw <= 0:
            return

        soc_drop = soc_before - soc_after
        if soc_drop <= 0:
            return

        energy_discharged_kwh = discharge_kw * duration_hours
        if energy_discharged_kwh <= 0:
            return

        soc_drop_rate = soc_drop / energy_discharged_kwh

        record = DischargeRecord(
            timestamp=datetime.now(),
            discharge_power_kw=discharge_kw,
            soc_before=soc_before,
            soc_after=soc_after,
            soc_drop_rate=soc_drop_rate,
            duration_hours=duration_hours,
        )

        bh.discharge_records.append(record)
        if len(bh.discharge_records) > cfg["max_discharge_records"]:
            bh.discharge_records = bh.discharge_records[-cfg["max_discharge_records"]:]

        self._update_internal_resistance_status(bes_id)

    def record_bess_charge(self, bes_id: str, charge_kw: float, duration_hours: float):
        bs = self.bess_state[bes_id]
        bh = bs.health

        if charge_kw <= 0:
            return

        energy_charged = charge_kw * duration_hours
        bh.total_charged_for_cycle += energy_charged

    def _update_cycle_count(self, bes_id: str, discharge_kw: float, duration_hours: float):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        if discharge_kw <= 0:
            return

        energy_discharged = discharge_kw * duration_hours
        bh.total_discharged_for_cycle += energy_discharged

        capacity_kwh = cfg["capacity_kwh"]
        full_cycle_energy = capacity_kwh

        cycles_added = energy_discharged / full_cycle_energy
        bh.equivalent_cycles += cycles_added

        self._update_health_percent(bes_id)
        self._check_life_warning(bes_id)

    def _update_health_percent(self, bes_id: str):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        cycle_threshold = cfg["cycle_life_threshold"]
        cycle_factor = max(0.0, 1.0 - bh.equivalent_cycles / cycle_threshold)

        resistance_factor = 0.85 if bh.internal_resistance_abnormal else 1.0

        bh.health_percent = max(0.0, 100.0 * cycle_factor * resistance_factor)

    def _check_life_warning(self, bes_id: str):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        if bh.equivalent_cycles >= cfg["cycle_life_threshold"]:
            self.add_alert(
                "BATTERY_LIFE_WARNING",
                f"电池{bes_id}等效循环次数已达{bh.equivalent_cycles:.1f}次，超过寿命阈值{cfg['cycle_life_threshold']}次",
                {"bes_id": bes_id, "equivalent_cycles": bh.equivalent_cycles,
                 "threshold": cfg["cycle_life_threshold"]}
            )

    def _update_internal_resistance_status(self, bes_id: str):
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        baseline_count = cfg["baseline_discharge_count"]
        recent_count = cfg["recent_discharge_count"]
        degradation_ratio = cfg["internal_resistance_degradation_ratio"]

        if len(bh.discharge_records) < baseline_count + recent_count:
            if len(bh.discharge_records) >= baseline_count and bh.baseline_soc_drop_rate is None:
                baseline_records = bh.discharge_records[:baseline_count]
                bh.baseline_soc_drop_rate = sum(r.soc_drop_rate for r in baseline_records) / baseline_count
            return

        if bh.baseline_soc_drop_rate is None:
            baseline_records = bh.discharge_records[:baseline_count]
            bh.baseline_soc_drop_rate = sum(r.soc_drop_rate for r in baseline_records) / baseline_count

        recent_records = bh.discharge_records[-recent_count:]
        recent_avg_rate = sum(r.soc_drop_rate for r in recent_records) / recent_count

        baseline_rate = bh.baseline_soc_drop_rate
        if baseline_rate <= 0:
            return

        rate_increase = (recent_avg_rate - baseline_rate) / baseline_rate

        was_abnormal = bh.internal_resistance_abnormal
        bh.internal_resistance_abnormal = rate_increase > degradation_ratio

        if bh.internal_resistance_abnormal and not was_abnormal:
            self.add_alert(
                "BATTERY_RESISTANCE_ALERT",
                f"电池{bes_id}内阻异常升高，最近{recent_count}次放电平均SOC下降速率比基线快{rate_increase*100:.1f}%",
                {"bes_id": bes_id, "baseline_rate": baseline_rate,
                 "recent_avg_rate": recent_avg_rate, "increase_ratio": rate_increase}
            )

        self._update_health_percent(bes_id)

    def get_battery_health_report(self, bes_id: str) -> Dict[str, Any]:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        self._update_health_percent(bes_id)
        self._update_internal_resistance_status(bes_id)

        recent_count = min(cfg["recent_discharge_count"], len(bh.discharge_records))
        recent_trend = []
        if recent_count > 0:
            recent_records = bh.discharge_records[-recent_count:]
            recent_trend = [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "discharge_power_kw": r.discharge_power_kw,
                    "soc_drop_rate": r.soc_drop_rate,
                }
                for r in recent_records
            ]

        estimated_remaining_cycles = max(0.0, cfg["cycle_life_threshold"] - bh.equivalent_cycles)

        return {
            "bes_id": bes_id,
            "equivalent_cycles": round(bh.equivalent_cycles, 2),
            "cycle_life_threshold": cfg["cycle_life_threshold"],
            "health_percent": round(bh.health_percent, 2),
            "internal_resistance_abnormal": bh.internal_resistance_abnormal,
            "baseline_soc_drop_rate": bh.baseline_soc_drop_rate,
            "recent_discharge_count": len(bh.discharge_records),
            "recent_soc_drop_trend": recent_trend,
            "estimated_remaining_cycles": round(estimated_remaining_cycles, 2),
            "power_derating_active": bh.health_percent < cfg["health_derating_threshold"],
        }

    def reset_baseline(self, bes_id: str) -> bool:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        baseline_count = cfg["baseline_discharge_count"]
        if len(bh.discharge_records) < baseline_count:
            return False

        recent_records = bh.discharge_records[-baseline_count:]
        bh.baseline_soc_drop_rate = sum(r.soc_drop_rate for r in recent_records) / baseline_count
        bh.internal_resistance_abnormal = False
        self._update_health_percent(bes_id)
        return True

    def get_bess_max_discharge_with_health(self, bes_id: str, time_interval_hours: float) -> float:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        self._update_health_percent(bes_id)

        base_max = self.get_bess_max_discharge(bes_id, time_interval_hours)

        if bh.health_percent < cfg["health_derating_threshold"]:
            derating = cfg["power_derating_ratio"]
            return base_max * (1.0 - derating)
        return base_max

    def get_bess_max_charge_with_health(self, bes_id: str, time_interval_hours: float) -> float:
        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        bh = bs.health

        self._update_health_percent(bes_id)

        base_max = self.get_bess_max_charge(bes_id, time_interval_hours)

        if bh.health_percent < cfg["health_derating_threshold"]:
            derating = cfg["power_derating_ratio"]
            return base_max * (1.0 - derating)
        return base_max

    def generate_storage_plan(self, now: datetime = None) -> StoragePlan:
        if now is None:
            now = datetime.now()

        plan_date = now.strftime("%Y-%m-%d")
        plan = StoragePlan(plan_date=plan_date, generated_at=now)

        valley_price = config.get_valley_price()
        peak_price = config.get_peak_price()
        charge_eff = config.BESS_CONFIG[list(config.BESS_CONFIG.keys())[0]]["charge_efficiency"]
        discharge_eff = config.BESS_CONFIG[list(config.BESS_CONFIG.keys())[0]]["discharge_efficiency"]
        min_ratio = config.STORAGE_STRATEGY_CONFIG["min_arbitrage_profit_ratio"]

        arbitrage_feasible = False
        if valley_price > 0:
            expected_revenue_per_kwh = peak_price * discharge_eff
            expected_cost_per_kwh = valley_price / charge_eff
            profit_ratio = (expected_revenue_per_kwh - expected_cost_per_kwh) / expected_cost_per_kwh
            if profit_ratio >= min_ratio:
                arbitrage_feasible = True

        for hour in range(24):
            period = config.get_tariff_period(hour)
            mode = config.get_storage_mode_for_hour(hour)

            if mode == "active_charge" and not arbitrage_feasible:
                mode = "normal"

            plan.hours[hour] = StoragePlanHour(
                hour=hour,
                mode=mode,
                tariff_period=period,
                active=True,
                abnormal=False,
            )

        self.current_storage_plan = plan
        self.last_plan_generation_date = plan_date
        return plan

    def should_generate_plan(self, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()

        if not config.STORAGE_STRATEGY_CONFIG["enable_strategy"]:
            return False

        today_str = now.strftime("%Y-%m-%d")
        if self.last_plan_generation_date == today_str and self.current_storage_plan is not None:
            return False

        gen_hour = self.plan_generation_hour
        gen_minute = self.plan_generation_minute

        if now.hour > gen_hour or (now.hour == gen_hour and now.minute >= gen_minute):
            return True

        return False

    def get_current_hour_plan(self, now: datetime = None) -> Optional[StoragePlanHour]:
        if now is None:
            now = datetime.now()
        if self.current_storage_plan is None:
            return None
        return self.current_storage_plan.hours.get(now.hour)

    def check_and_handle_soc_abnormal(self, bes_id: str, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()
        if self.current_storage_plan is None:
            return False

        cfg = config.BESS_CONFIG[bes_id]
        bs = self.bess_state[bes_id]
        current_plan = self.current_storage_plan.hours.get(now.hour)

        if current_plan is None:
            return False

        if bs.soc < cfg["soc_min"]:
            if not current_plan.abnormal:
                current_plan.abnormal = True
                current_plan.active = False
                self.add_alert(
                    "STORAGE_PLAN_SUSPENDED",
                    f"电池{bes_id} SOC({bs.soc*100:.1f}%)低于下限({cfg['soc_min']*100:.1f}%)，当前时段({now.hour}时)储能计划暂停",
                    {"bes_id": bes_id, "soc": bs.soc, "soc_min": cfg["soc_min"], "hour": now.hour}
                )
            return True
        else:
            if current_plan.abnormal:
                current_plan.abnormal = False
                current_plan.active = True
                self.add_alert(
                    "STORAGE_PLAN_RESUMED",
                    f"电池{bes_id} SOC恢复至安全区间，当前时段({now.hour}时)储能计划恢复执行",
                    {"bes_id": bes_id, "soc": bs.soc, "hour": now.hour}
                )
        return False

    def get_storage_plan_report(self) -> Dict[str, Any]:
        if self.current_storage_plan is None:
            return {
                "plan_exists": False,
                "message": "暂无生效的储能计划",
            }

        hours_info = []
        for h in range(24):
            hp = self.current_storage_plan.hours.get(h)
            if hp:
                hours_info.append({
                    "hour": h,
                    "mode": hp.mode,
                    "mode_chinese": {
                        "active_charge": "主动充电",
                        "priority_discharge": "优先放电",
                        "normal": "常规模式",
                    }.get(hp.mode, "未知"),
                    "tariff_period": hp.tariff_period,
                    "period_chinese": {
                        "valley": "谷时段",
                        "flat": "平时段",
                        "peak": "峰时段",
                    }.get(hp.tariff_period, "未知"),
                    "active": hp.active,
                    "abnormal": hp.abnormal,
                })

        return {
            "plan_exists": True,
            "plan_date": self.current_storage_plan.plan_date,
            "generated_at": self.current_storage_plan.generated_at.isoformat(),
            "plan_generation_time": f"{self.plan_generation_hour:02d}:{self.plan_generation_minute:02d}",
            "hours": hours_info,
        }

    def get_arbitrage_stats_report(self) -> Dict[str, Any]:
        a = self.stats.arbitrage
        net_profit = a.total_arbitrage_revenue - a.total_arbitrage_cost
        return {
            "total_arbitrage_charge_kwh": round(a.total_arbitrage_charge_kwh, 4),
            "total_arbitrage_discharge_kwh": round(a.total_arbitrage_discharge_kwh, 4),
            "total_arbitrage_cost": round(a.total_arbitrage_cost, 4),
            "total_arbitrage_revenue": round(a.total_arbitrage_revenue, 4),
            "net_profit": round(net_profit, 4),
            "charge_count": int(a.total_arbitrage_charge_kwh > 0),
        }

    def update_plan_generation_time(self, hour: int, minute: int) -> bool:
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False
        self.plan_generation_hour = hour
        self.plan_generation_minute = minute
        config.STORAGE_STRATEGY_CONFIG["plan_generation_hour"] = hour
        config.STORAGE_STRATEGY_CONFIG["plan_generation_minute"] = minute
        return True

    def record_arbitrage_charge(self, energy_kwh: float, cost: float):
        self.stats.arbitrage.total_arbitrage_charge_kwh += energy_kwh
        self.stats.arbitrage.total_arbitrage_cost += cost

    def record_arbitrage_discharge(self, energy_kwh: float, revenue: float):
        self.stats.arbitrage.total_arbitrage_discharge_kwh += energy_kwh
        self.stats.arbitrage.total_arbitrage_revenue += revenue

    def _get_rated_power(self, source_type: str, source_id: str) -> float:
        if source_type == "pv":
            return config.PV_CONFIG.get(source_id, {}).get("rated_power", 0.0)
        elif source_type == "wt":
            return config.WT_CONFIG.get(source_id, {}).get("rated_power", 0.0)
        elif source_type == "diesel":
            return config.DIESEL_CONFIG.get(source_id, {}).get("rated_power", 0.0)
        return 0.0

    def _compute_fluctuation_penalty(self, health_key: str, report: SourceReport) -> float:
        hs = self.source_health[health_key]
        rated = self._get_rated_power(hs.source_type, hs.source_id)
        if rated <= 0:
            return 0.0

        recent_available = [h for h in hs.history[-10:] if h.available and h.power_kw > 0]
        if len(recent_available) < 2:
            return 0.0

        powers = [h.power_kw for h in recent_available]
        avg_power = sum(powers) / len(powers)
        if avg_power <= 0:
            return 0.0

        max_deviation = max(abs(p - avg_power) for p in powers)
        fluctuation_ratio = max_deviation / rated

        if fluctuation_ratio > self.FLUCTUATION_HIGH:
            return 20.0
        return 0.0

    def _update_source_health(self, health_key: str, report: SourceReport):
        hs = self.source_health[health_key]
        if hs.in_maintenance:
            return

        new_score = 100.0

        if not report.available:
            hs.consecutive_unavailable += 1
            if hs.consecutive_unavailable >= 2:
                new_score = 0.0
            else:
                new_score = 60.0
        else:
            hs.consecutive_unavailable = 0
            fluct_penalty = self._compute_fluctuation_penalty(health_key, report)
            new_score = 100.0 - fluct_penalty

        hs.health_score = max(0.0, min(100.0, new_score))

        if hs.health_score <= self.HEALTH_DANGER_THRESHOLD:
            new_status = "danger"
        elif hs.health_score <= self.HEALTH_WARNING_THRESHOLD:
            new_status = "warning"
        else:
            new_status = "normal"

        old_status = hs.status
        hs.status = new_status

        record = SourceHealthRecord(
            timestamp=report.timestamp,
            available=report.available,
            power_kw=report.power_kw,
            health_score=hs.health_score,
            status=hs.status,
        )
        hs.history.append(record)
        if len(hs.history) > self.HEALTH_WINDOW_SIZE:
            hs.history = hs.history[-self.HEALTH_WINDOW_SIZE:]

        if old_status != new_status and new_status in ("warning", "danger"):
            level_text = "预警" if new_status == "warning" else "高危"
            self.add_alert(
                f"SOURCE_{new_status.upper()}",
                f"发电源 {report.source_type}:{report.source_id} 进入{level_text}状态，健康评分 {hs.health_score:.1f}",
                {
                    "source_type": hs.source_type,
                    "source_id": hs.source_id,
                    "health_score": hs.health_score,
                    "status": hs.status,
                }
            )

    def _handle_fault_transition(self, health_key: str, report: SourceReport):
        hs = self.source_health[health_key]
        if hs.in_maintenance:
            return

        if not report.available and hs.current_fault is None:
            self._fault_event_counter += 1
            event_id = f"FAULT-{self._fault_event_counter:06d}"
            plan = self.backup_plans.get(health_key)
            event = FaultEvent(
                event_id=event_id,
                source_id=hs.source_id,
                source_type=hs.source_type,
                started_at=report.timestamp,
                had_plan=plan is not None,
                plan_id=plan.plan_id if plan else None,
            )
            self.fault_events.append(event)
            hs.current_fault = {"event_id": event_id, "started_at": report.timestamp.isoformat()}

            if plan is None:
                self.add_alert(
                    "SOURCE_UNEXPECTED_FAULT",
                    f"发电源 {hs.source_type}:{hs.source_id} 突发掉线，无预案覆盖，请紧急处理",
                    {
                        "source_type": hs.source_type,
                        "source_id": hs.source_id,
                        "event_id": event_id,
                    }
                )

        if report.available and hs.current_fault is not None:
            event_id = hs.current_fault["event_id"]
            for ev in self.fault_events:
                if ev.event_id == event_id:
                    ev.ended_at = report.timestamp
                    ev.duration_minutes = (report.timestamp - ev.started_at).total_seconds() / 60.0
                    break
            hs.current_fault = None
            hs.status = "normal"
            hs.health_score = 100.0
            hs.consecutive_unavailable = 0

    def _generate_backup_plan(self, health_key: str):
        hs = self.source_health[health_key]
        rated = self._get_rated_power(hs.source_type, hs.source_id)
        current_load = self.get_load_kw()

        remaining_renewable = 0.0
        for sid, r in self.pv_reports.items():
            if f"pv:{sid}" != health_key and r.available:
                remaining_renewable += max(0.0, r.power_kw)
        for sid, r in self.wt_reports.items():
            if f"wt:{sid}" != health_key and r.available:
                remaining_renewable += max(0.0, r.power_kw)

        self._backup_plan_counter += 1
        plan_id = f"PLAN-{self._backup_plan_counter:06d}"

        alternatives = {
            "renewable_kw": round(remaining_renewable, 2),
            "battery_max_discharge_kw": 0.0,
            "diesel_max_kw": 0.0,
            "grid_import_available": True,
        }

        bes_id = list(config.BESS_CONFIG.keys())[0]
        ds_id = list(config.DIESEL_CONFIG.keys())[0]
        time_interval = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0

        max_batt_discharge = self.get_bess_max_discharge_with_health(bes_id, time_interval)
        alternatives["battery_max_discharge_kw"] = round(max_batt_discharge, 2)

        now = datetime.now()
        diesel_cap = self.get_available_diesel_capacity(ds_id, now)
        alternatives["diesel_max_kw"] = round(diesel_cap.get("max_output", 0.0), 2)
        alternatives["diesel_can_start"] = diesel_cap.get("can_run", False)

        total_available = remaining_renewable + alternatives["battery_max_discharge_kw"] + alternatives["diesel_max_kw"]
        gap = max(0.0, current_load - total_available)

        suggestions = []
        if gap > 0:
            suggestions.append(f"功率缺口 {gap:.2f}kW")
            if not alternatives["diesel_can_start"]:
                suggestions.append("建议提前启动柴油机暖机")
            cfg = config.BESS_CONFIG[bes_id]
            bs = self.bess_state[bes_id]
            if bs.soc < cfg["soc_max"] * 0.9:
                suggestions.append(f"建议提前将电池充至高SOC (当前 {bs.soc*100:.1f}%)")
        else:
            batt_share = min(max_batt_discharge, max(0.0, current_load - remaining_renewable))
            after_batt = max(0.0, current_load - remaining_renewable - batt_share)
            diesel_share = min(alternatives["diesel_max_kw"], after_batt)
            after_diesel = max(0.0, after_batt - diesel_share)
            grid_share = after_diesel

            alternatives["breakdown"] = {
                "battery_discharge_kw": round(batt_share, 2),
                "diesel_output_kw": round(diesel_share, 2),
                "grid_import_kw": round(grid_share, 2),
            }
            suggestions.append("预案可覆盖负荷缺口")

        plan = BackupPlan(
            plan_id=plan_id,
            source_id=hs.source_id,
            source_type=hs.source_type,
            generated_at=datetime.now(),
            can_cover=gap <= 0,
            gap_kw=round(gap, 2),
            suggestions=suggestions,
            alternative_sources=alternatives,
            load_kw=round(current_load, 2),
            lost_capacity_kw=round(rated, 2),
        )
        self.backup_plans[health_key] = plan

    def get_active_backup_plans(self) -> List[BackupPlan]:
        return [p for p in self.backup_plans.values()]

    def get_backup_plan_for_source(self, health_key: str) -> Optional[BackupPlan]:
        return self.backup_plans.get(health_key)

    def get_source_health_status(self, health_key: str) -> Optional[Dict[str, Any]]:
        hs = self.source_health.get(health_key)
        if hs is None:
            return None
        return {
            "source_type": hs.source_type,
            "source_id": hs.source_id,
            "health_score": round(hs.health_score, 2),
            "status": hs.status,
            "status_chinese": {"normal": "正常", "warning": "预警", "danger": "高危"}.get(hs.status, "未知"),
            "in_maintenance": hs.in_maintenance,
            "maintenance_started_at": hs.maintenance_started_at.isoformat() if hs.maintenance_started_at else None,
            "consecutive_unavailable": hs.consecutive_unavailable,
            "current_fault": hs.current_fault,
            "history_count": len(hs.history),
        }

    def get_source_health_history(self, health_key: str, limit: int = 50) -> List[Dict[str, Any]]:
        hs = self.source_health.get(health_key)
        if hs is None:
            return []
        history = hs.history[-limit:]
        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "available": r.available,
                "power_kw": r.power_kw,
                "health_score": round(r.health_score, 2),
                "status": r.status,
            }
            for r in history
        ]

    def set_source_maintenance(self, health_key: str, in_maintenance: bool) -> bool:
        hs = self.source_health.get(health_key)
        if hs is None:
            return False
        hs.in_maintenance = in_maintenance
        if in_maintenance:
            hs.maintenance_started_at = datetime.now()
            hs.status = "normal"
            hs.health_score = 100.0
            hs.consecutive_unavailable = 0
            hs.current_fault = None
            if health_key in self.backup_plans:
                del self.backup_plans[health_key]
            self.add_alert(
                "SOURCE_MAINTENANCE_START",
                f"发电源 {hs.source_type}:{hs.source_id} 进入维护状态",
                {"source_type": hs.source_type, "source_id": hs.source_id}
            )
        else:
            hs.maintenance_started_at = None
            self.add_alert(
                "SOURCE_MAINTENANCE_END",
                f"发电源 {hs.source_type}:{hs.source_id} 结束维护状态",
                {"source_type": hs.source_type, "source_id": hs.source_id}
            )
        return True

    def get_fault_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        events = self.fault_events[-limit:]
        result = []
        for ev in reversed(events):
            result.append({
                "event_id": ev.event_id,
                "source_type": ev.source_type,
                "source_id": ev.source_id,
                "started_at": ev.started_at.isoformat(),
                "ended_at": ev.ended_at.isoformat() if ev.ended_at else None,
                "duration_minutes": round(ev.duration_minutes, 2) if ev.ended_at else None,
                "still_active": ev.ended_at is None,
                "had_plan": ev.had_plan,
                "plan_id": ev.plan_id,
            })
        return result

    def generate_dispatch_id(self) -> str:
        self._dispatch_counter += 1
        return f"DISP-{self._dispatch_counter:08d}"

    def generate_audit_id(self) -> str:
        self._audit_counter += 1
        return f"AUDIT-{self._audit_counter:08d}"

    def add_audit_log(self, audit_log: AuditLog):
        self.audit_logs.append(audit_log)

    def get_audit_log(self, audit_id: str) -> Optional[AuditLog]:
        for log in self.audit_logs:
            if log.audit_id == audit_id:
                return log
        return None

    def get_audit_log_by_dispatch_id(self, dispatch_id: str) -> Optional[AuditLog]:
        for log in self.audit_logs:
            if log.dispatch_id == dispatch_id:
                return log
        return None

    def query_audit_logs(self,
                         start_time: Optional[datetime] = None,
                         end_time: Optional[datetime] = None,
                         min_cost: Optional[float] = None,
                         has_load_shed: Optional[bool] = None,
                         has_diesel_start: Optional[bool] = None,
                         has_anomaly: Optional[bool] = None,
                         limit: int = 50,
                         offset: int = 0) -> List[AuditLog]:
        result = []
        for log in self.audit_logs:
            if start_time and log.timestamp < start_time:
                continue
            if end_time and log.timestamp > end_time:
                continue
            if min_cost is not None and log.output_summary.total_cost < min_cost:
                continue
            if has_load_shed is not None:
                if has_load_shed and log.output_summary.load_shed_kw <= 0:
                    continue
                if not has_load_shed and log.output_summary.load_shed_kw > 0:
                    continue
            if has_diesel_start is not None:
                if has_diesel_start and log.output_summary.diesel_share_kw <= 0:
                    continue
                if not has_diesel_start and log.output_summary.diesel_share_kw > 0:
                    continue
            if has_anomaly is not None:
                if has_anomaly and not log.has_anomaly():
                    continue
                if not has_anomaly and log.has_anomaly():
                    continue
            result.append(log)

        result = list(reversed(result))
        total = len(result)
        start_idx = offset
        end_idx = min(offset + limit, total)
        return result[start_idx:end_idx]

    def get_anomaly_audit_logs(self, limit: int = 50) -> List[AuditLog]:
        anomalies = [log for log in self.audit_logs if log.has_anomaly()]
        anomalies = list(reversed(anomalies))
        return anomalies[:limit]

    def compute_priority_load_shedding(self, gap_kw: float, now: datetime,
                                       dispatch_id: str = None) -> Tuple[Dict[str, float], float]:
        if gap_kw <= 0:
            for gid, gs in self.load_group_state.items():
                reported = max(0.0, gs["reported_power_kw"])
                gs["current_served_kw"] = max(0.0, reported - gs["current_shed_kw"])
            return {}, 0.0

        remaining_gap = gap_kw
        shed_by_group: Dict[str, float] = {}

        for gid, gs in self.load_group_state.items():
            reported = max(0.0, gs["reported_power_kw"])
            gs["current_served_kw"] = max(0.0, reported - gs["current_shed_kw"])

        sorted_by_shed = sorted(
            self.load_group_state.items(),
            key=lambda x: x[1]["shed_priority"],
            reverse=True,
        )

        for gid, gs in sorted_by_shed:
            if remaining_gap <= 0:
                break

            reported = max(0.0, gs["reported_power_kw"])
            currently_served = reported - gs["current_shed_kw"]
            max_shed_allowed = reported * gs["max_shed_ratio"]
            additional_can_shed = max(0.0, max_shed_allowed - gs["current_shed_kw"])
            can_serve_min = max(0.0, reported - max_shed_allowed)
            additional_shed = min(remaining_gap, additional_can_shed, currently_served - can_serve_min)

            if additional_shed > 0.01:
                shed_by_group[gid] = additional_shed
                remaining_gap -= additional_shed
                new_total_shed = gs["current_shed_kw"] + additional_shed
                gs["current_shed_kw"] = new_total_shed
                gs["current_served_kw"] = max(0.0, reported - new_total_shed)

                if gid not in self._active_shed_events or self._active_shed_events[gid].shed_power_kw <= 0:
                    self._load_group_event_counter += 1
                    event_id = f"SHED-{self._load_group_event_counter:06d}"
                    event = LoadGroupShedEvent(
                        event_id=event_id,
                        group_id=gid,
                        group_name=gs["name"],
                        shed_power_kw=new_total_shed,
                        started_at=now,
                        reason=f"供电缺口{gap_kw:.2f}kW，按优先级切除负荷",
                        dispatch_id=dispatch_id,
                    )
                    self.load_group_shed_events.append(event)
                    self._active_shed_events[gid] = event
                else:
                    event = self._active_shed_events[gid]
                    event.shed_power_kw = new_total_shed
            elif gid in self._active_shed_events and self._active_shed_events[gid].shed_power_kw > 0:
                event = self._active_shed_events[gid]
                event.shed_power_kw = gs["current_shed_kw"]

        total_shed = sum(shed_by_group.values())
        return shed_by_group, remaining_gap

    def restore_load_groups(self, surplus_kw: float, now: datetime,
                            dispatch_id: str = None) -> Tuple[Dict[str, float], float]:
        if surplus_kw <= 0:
            return {}, 0.0

        remaining_surplus = surplus_kw
        restored_by_group: Dict[str, float] = {}

        sorted_by_restore = sorted(
            self.load_group_state.items(),
            key=lambda x: x[1]["restore_priority"],
        )

        for gid, gs in sorted_by_restore:
            if remaining_surplus <= 0:
                break
            if gs["current_shed_kw"] <= 0:
                continue

            restore_amount = min(remaining_surplus, gs["current_shed_kw"])
            if restore_amount > 0.01:
                restored_by_group[gid] = restore_amount
                remaining_surplus -= restore_amount
                new_shed = gs["current_shed_kw"] - restore_amount
                gs["current_shed_kw"] = new_shed
                reported = max(0.0, gs["reported_power_kw"])
                gs["current_served_kw"] = reported - new_shed

                if gid in self._active_shed_events:
                    event = self._active_shed_events[gid]
                    if new_shed <= 0.01:
                        event.ended_at = now
                        event.duration_minutes = (now - event.started_at).total_seconds() / 60.0
                        del self._active_shed_events[gid]
                    else:
                        event.shed_power_kw = new_shed

        total_restored = sum(restored_by_group.values())
        return restored_by_group, remaining_surplus

    def finalize_group_state_after_dispatch(self):
        for gid, gs in self.load_group_state.items():
            reported = max(0.0, gs["reported_power_kw"])
            if gs["current_shed_kw"] > reported:
                gs["current_shed_kw"] = reported
            gs["current_served_kw"] = reported - gs["current_shed_kw"]

    def record_reliability_snapshot(self, now: datetime):
        for gid, gs in self.load_group_state.items():
            snapshot = LoadGroupReliabilitySnapshot(
                timestamp=now,
                group_id=gid,
                is_normal=gs["current_shed_kw"] <= 0.01,
                shed_power_kw=gs["current_shed_kw"],
            )
            self.load_group_reliability_history[gid].append(snapshot)
            if len(self.load_group_reliability_history[gid]) > 10000:
                self.load_group_reliability_history[gid] = self.load_group_reliability_history[gid][-10000:]

    def get_load_group_status(self, group_id: str = None) -> Dict[str, Any]:
        result = {}
        groups = [group_id] if group_id else list(self.load_group_state.keys())
        for gid in groups:
            if gid not in self.load_group_state:
                continue
            gs = self.load_group_state[gid]
            reported = max(0.0, gs["reported_power_kw"])
            shed = gs["current_shed_kw"]
            if shed <= 0.01:
                supply_status = "正常"
            elif shed >= reported - 0.01 and reported > 0:
                supply_status = "完全切除"
            else:
                supply_status = "部分切除"

            max_shed_allowed = reported * gs["max_shed_ratio"]
            result[gid] = {
                "group_id": gid,
                "name": gs["name"],
                "description": gs["description"],
                "rated_power_kw": gs["rated_power_kw"],
                "reported_power_kw": round(reported, 2),
                "max_shed_ratio": gs["max_shed_ratio"],
                "max_shed_allowed_kw": round(max_shed_allowed, 2),
                "shed_priority": gs["shed_priority"],
                "restore_priority": gs["restore_priority"],
                "current_served_kw": round(gs["current_served_kw"], 2),
                "current_shed_kw": round(shed, 2),
                "supply_status": supply_status,
                "shed_ratio_of_reported": round(shed / reported, 4) if reported > 0 else 0,
                "shed_ratio_of_allowed": round(shed / max_shed_allowed, 4) if max_shed_allowed > 0 else 0,
                "last_report_time": gs["last_report_time"].isoformat() if gs["last_report_time"] else None,
                "is_actively_shed": gid in self._active_shed_events,
            }
        if group_id:
            return result.get(group_id, {})
        return result

    def update_load_group_config(self, group_id: str, rated_power_kw: float = None,
                                  max_shed_ratio: float = None) -> bool:
        if group_id not in self.load_group_state:
            return False

        gs = self.load_group_state[group_id]
        if rated_power_kw is not None:
            if rated_power_kw < 0:
                return False
            gs["rated_power_kw"] = float(rated_power_kw)
            config.LOAD_GROUP_CONFIG[group_id]["rated_power_kw"] = float(rated_power_kw)

        if max_shed_ratio is not None:
            if not (0 <= max_shed_ratio <= 1):
                return False
            if group_id == "group1" and max_shed_ratio > 0:
                return False
            gs["max_shed_ratio"] = float(max_shed_ratio)
            config.LOAD_GROUP_CONFIG[group_id]["max_shed_ratio"] = float(max_shed_ratio)

        return True

    def get_load_group_shed_history(self, group_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        if group_id:
            events = [e for e in self.load_group_shed_events if e.group_id == group_id]
        else:
            events = list(self.load_group_shed_events)
        events = events[-limit:]
        result = []
        for e in reversed(events):
            result.append({
                "event_id": e.event_id,
                "group_id": e.group_id,
                "group_name": e.group_name,
                "shed_power_kw": round(e.shed_power_kw, 2),
                "started_at": e.started_at.isoformat(),
                "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                "duration_minutes": round(e.duration_minutes, 2) if e.ended_at else None,
                "still_active": e.ended_at is None,
                "reason": e.reason,
                "dispatch_id": e.dispatch_id,
            })
        return result

    def get_load_group_reliability_stats(self, group_id: str = None,
                                          start_time: datetime = None,
                                          end_time: datetime = None) -> Dict[str, Any]:
        result = {}
        groups = [group_id] if group_id else list(self.load_group_state.keys())

        for gid in groups:
            history = self.load_group_reliability_history.get(gid, [])
            if start_time:
                history = [h for h in history if h.timestamp >= start_time]
            if end_time:
                history = [h for h in history if h.timestamp <= end_time]

            if not history:
                result[gid] = {
                    "group_id": gid,
                    "name": self.load_group_state.get(gid, {}).get("name", gid),
                    "total_snapshots": 0,
                    "normal_snapshots": 0,
                    "shed_snapshots": 0,
                    "reliability_percent": None,
                    "avg_shed_power_kw": 0,
                    "max_shed_power_kw": 0,
                    "time_window": {
                        "start": start_time.isoformat() if start_time else None,
                        "end": end_time.isoformat() if end_time else None,
                    },
                }
                continue

            total = len(history)
            normal = sum(1 for h in history if h.is_normal)
            shed_count = total - normal
            reliability = normal / total * 100 if total > 0 else 0
            avg_shed = sum(h.shed_power_kw for h in history) / total
            max_shed = max(h.shed_power_kw for h in history)

            result[gid] = {
                "group_id": gid,
                "name": self.load_group_state.get(gid, {}).get("name", gid),
                "total_snapshots": total,
                "normal_snapshots": normal,
                "shed_snapshots": shed_count,
                "reliability_percent": round(reliability, 2),
                "avg_shed_power_kw": round(avg_shed, 2),
                "max_shed_power_kw": round(max_shed, 2),
                "time_window": {
                    "start": history[0].timestamp.isoformat(),
                    "end": history[-1].timestamp.isoformat(),
                },
            }

        if group_id:
            return result.get(group_id, {})
        return result

    def generate_load_group_event_id(self) -> str:
        self._load_group_event_counter += 1
        return f"SHED-{self._load_group_event_counter:06d}"
