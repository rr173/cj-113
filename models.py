from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
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
class LoadReport:
    load_kw: float
    timestamp: datetime


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
class BessState:
    soc: float
    charge_power_kw: float = 0.0
    discharge_power_kw: float = 0.0
    total_charged_kwh: float = 0.0
    total_discharged_kwh: float = 0.0


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


class MicrogridState:
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

    def report_source(self, report: SourceReport):
        if report.source_type == "pv":
            self.pv_reports[report.source_id] = report
        elif report.source_type == "wt":
            self.wt_reports[report.source_id] = report
        elif report.source_type == "diesel":
            self.diesel_reports[report.source_id] = report

    def report_load(self, report: LoadReport):
        self.load_report = report

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
