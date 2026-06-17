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
class DischargeRecord:
    timestamp: datetime
    discharge_power_kw: float
    soc_before: float
    soc_after: float
    soc_drop_rate: float
    duration_hours: float


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

        self.current_storage_plan: Optional[StoragePlan] = None
        self.last_plan_generation_date: Optional[str] = None
        self.plan_generation_hour: int = config.STORAGE_STRATEGY_CONFIG["plan_generation_hour"]
        self.plan_generation_minute: int = config.STORAGE_STRATEGY_CONFIG["plan_generation_minute"]

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
