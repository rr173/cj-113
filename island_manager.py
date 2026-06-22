from __future__ import annotations
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

import config


class OperationMode(Enum):
    GRID_CONNECTED = "grid_connected"
    ISLAND = "island"
    BLACK_START = "black_start"


class BlackStartStep(Enum):
    WAITING = "waiting"
    SOURCE_STABILITY_CHECK = "source_stability_check"
    LOAD_RESTORE = "load_restore"
    SOC_RECOVERY = "soc_recovery"
    COMPLETED = "completed"


@dataclass
class IslandEvent:
    event_id: str
    entered_at: datetime
    exited_at: Optional[datetime] = None
    duration_minutes: float = 0.0
    total_shed_kwh: float = 0.0
    total_diesel_consumption_kwh: float = 0.0
    total_diesel_consumption_liters: float = 0.0
    survived: bool = True
    blackout_occurred: bool = False
    blackout_at: Optional[datetime] = None
    black_start_started_at: Optional[datetime] = None
    black_start_completed_at: Optional[datetime] = None
    black_start_duration_minutes: float = 0.0
    period_shed_kwh: List[Dict[str, Any]] = field(default_factory=list)
    period_diesel_kwh: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SourceStabilityRecord:
    timestamp: datetime
    total_renewable_kw: float
    diesel_kw: float


@dataclass
class BlackStartProgress:
    current_step: BlackStartStep
    source_stability_consecutive_count: int
    source_stability_required: int
    source_stability_met: bool
    load_restore_progress_percent: float
    load_restore_total_shed_kw: float
    load_restore_restored_kw: float
    load_restore_remaining_kw: float
    soc_current: float
    soc_target: float
    soc_met: bool
    all_conditions_met: bool
    details: Dict[str, Any] = field(default_factory=dict)


class IslandManager:
    def __init__(self, state):
        self.state = state
        self.mode: OperationMode = OperationMode.GRID_CONNECTED
        self.island_event_counter: int = 0
        self.current_island_event: Optional[IslandEvent] = None
        self.island_events: List[IslandEvent] = []

        self.black_start_step: BlackStartStep = BlackStartStep.WAITING
        self.source_stability_records: List[SourceStabilityRecord] = []
        self.source_stability_consecutive_count: int = 0
        self.source_stability_required: int = 3
        self.source_stability_fluctuation_threshold: float = 0.10

        self.shed_load_at_island_entry: Dict[str, float] = {}
        self.load_restore_total_shed_kw: float = 0.0
        self.load_restore_restored_kw: float = 0.0
        self.load_restore_max_rate_per_cycle: float = 0.25

        cfg = config.ISLAND_MODE_CONFIG
        self.island_soc_min: float = cfg["island_soc_min"]
        self.black_start_soc_target: float = cfg["black_start_soc_target"]
        self.source_stability_required: int = cfg["source_stability_required_periods"]
        self.source_stability_fluctuation_threshold: float = cfg["source_stability_fluctuation_threshold"]
        self.load_restore_max_rate_per_cycle: float = cfg["load_restore_max_rate_per_cycle"]

        self._island_period_shed_kw: float = 0.0
        self._island_period_diesel_kw: float = 0.0

    def get_mode(self) -> OperationMode:
        return self.mode

    def get_mode_name(self) -> str:
        return {
            OperationMode.GRID_CONNECTED: "并网模式",
            OperationMode.ISLAND: "孤岛模式",
            OperationMode.BLACK_START: "黑启动中",
        }.get(self.mode, "未知")

    def get_island_soc_min(self) -> float:
        if self.mode in (OperationMode.ISLAND, OperationMode.BLACK_START):
            return self.island_soc_min
        return config.BESS_CONFIG[list(config.BESS_CONFIG.keys())[0]]["soc_min"]

    def is_grid_allowed(self) -> bool:
        return self.mode == OperationMode.GRID_CONNECTED

    def trigger_grid_outage(self, now: datetime = None) -> Dict[str, Any]:
        if now is None:
            now = datetime.now()

        if self.mode == OperationMode.ISLAND:
            return {"success": False, "error": "已在孤岛模式中，无法重复触发"}

        if self.mode == OperationMode.BLACK_START:
            return {"success": False, "error": "正在黑启动流程中，无法触发孤岛"}

        previous_mode = self.mode
        self.mode = OperationMode.ISLAND

        self.island_event_counter += 1
        event_id = f"ISLAND-{self.island_event_counter:06d}"
        event = IslandEvent(event_id=event_id, entered_at=now)
        self.current_island_event = event
        self.island_events.append(event)

        self.source_stability_records = []
        self.source_stability_consecutive_count = 0
        self.shed_load_at_island_entry = {}
        self.load_restore_restored_kw = 0.0
        self.load_restore_total_shed_kw = 0.0
        self._island_period_shed_kw = 0.0
        self._island_period_diesel_kw = 0.0

        active_sheds = {
            gid: ev.shed_power_kw
            for gid, ev in self.state._active_shed_events.items()
            if ev.shed_power_kw > 0.01
        }
        self.shed_load_at_island_entry = dict(active_sheds)

        self.state.add_alert(
            "ISLAND_MODE_ENTERED",
            f"电网停电，系统切换至孤岛模式运行，禁止购电和售电，SOC下限提升至{self.island_soc_min*100:.0f}%",
            {"entered_at": now.isoformat(), "event_id": event_id,
             "previous_mode": previous_mode.value}
        )

        return {
            "success": True,
            "mode": self.mode.value,
            "mode_chinese": self.get_mode_name(),
            "event_id": event_id,
            "entered_at": now.isoformat(),
            "island_soc_min": self.island_soc_min,
            "existing_shed_groups": list(self.shed_load_at_island_entry.keys()),
        }

    def trigger_grid_recovery(self, now: datetime = None) -> Dict[str, Any]:
        if now is None:
            now = datetime.now()

        if self.mode != OperationMode.ISLAND:
            return {"success": False, "error": "当前不在孤岛模式，无法触发电网恢复"}

        self.mode = OperationMode.BLACK_START
        self.black_start_step = BlackStartStep.SOURCE_STABILITY_CHECK
        self.source_stability_records = []
        self.source_stability_consecutive_count = 0

        if self.current_island_event:
            self.current_island_event.black_start_started_at = now

        self.load_restore_total_shed_kw = self._compute_current_total_shed_kw()

        self.state.add_alert(
            "BLACK_START_INITIATED",
            f"电网恢复，进入黑启动流程，开始确认新能源和柴油机出力稳定性",
            {"started_at": now.isoformat(), "step": "source_stability_check",
             "total_shed_kw": self.load_restore_total_shed_kw}
        )

        return {
            "success": True,
            "mode": self.mode.value,
            "mode_chinese": self.get_mode_name(),
            "black_start_step": self.black_start_step.value,
            "started_at": now.isoformat(),
            "total_shed_kw_to_restore": self.load_restore_total_shed_kw,
        }

    def update_dispatch_cycle(self, now: datetime = None,
                               total_renewable_kw: float = 0.0,
                               diesel_kw: float = 0.0,
                               load_shed_kw: float = 0.0,
                               diesel_generated_kwh: float = 0.0) -> Dict[str, Any]:
        if now is None:
            now = datetime.now()

        result = {
            "mode": self.mode.value,
            "actions_taken": [],
        }

        if self.mode == OperationMode.ISLAND:
            self._record_island_period_stats(now, load_shed_kw, diesel_generated_kwh)
            self._check_blackout(now)

        elif self.mode == OperationMode.BLACK_START:
            self._record_island_period_stats(now, load_shed_kw, diesel_generated_kwh)
            step_result = self._advance_black_start(now, total_renewable_kw, diesel_kw)
            result["black_start_update"] = step_result

        return result

    def _record_island_period_stats(self, now: datetime, load_shed_kw: float, diesel_generated_kwh: float):
        if self.current_island_event is None:
            return

        self._island_period_shed_kw = load_shed_kw
        self._island_period_diesel_kw = diesel_generated_kwh

        self.current_island_event.total_shed_kwh += load_shed_kw * (config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0)
        self.current_island_event.total_diesel_consumption_kwh += diesel_generated_kwh

        diesel_efficiency_kwh_per_liter = config.ISLAND_MODE_CONFIG.get(
            "diesel_efficiency_kwh_per_liter", 3.5
        )
        self.current_island_event.total_diesel_consumption_liters = (
            self.current_island_event.total_diesel_consumption_kwh / diesel_efficiency_kwh_per_liter
        )

    def _check_blackout(self, now: datetime):
        if self.current_island_event is None or self.current_island_event.blackout_occurred:
            return

        bes_id = list(config.BESS_CONFIG.keys())[0]
        bs = self.state.bess_state[bes_id]
        soc_min = self.island_soc_min

        battery_depleted = bs.soc <= soc_min

        ds_id = list(config.DIESEL_CONFIG.keys())[0]
        ds = self.state.diesel_state[ds_id]
        diesel_report = self.state.diesel_reports.get(ds_id)
        diesel_unavailable = not ds.running or (diesel_report and not diesel_report.available)

        total_renewable = self.state.get_total_renewable_kw()
        load_kw = self.state.get_load_kw()
        supply_gap = load_kw - total_renewable
        if supply_gap <= 0:
            return

        if battery_depleted and diesel_unavailable:
            self.current_island_event.blackout_occurred = True
            self.current_island_event.blackout_at = now
            self.current_island_event.survived = False

            self.state.add_alert(
                "TOTAL_BLACKOUT",
                f"全黑事件：电池SOC耗尽({bs.soc*100:.1f}%)且柴油机不可用，微网完全失去供电能力",
                {"soc": bs.soc, "diesel_running": ds.running, "timestamp": now.isoformat()}
            )

    def _advance_black_start(self, now: datetime,
                              total_renewable_kw: float, diesel_kw: float) -> Dict[str, Any]:
        result = {"step": self.black_start_step.value, "advanced": False}

        if self.black_start_step == BlackStartStep.SOURCE_STABILITY_CHECK:
            return self._check_source_stability(now, total_renewable_kw, diesel_kw)

        elif self.black_start_step == BlackStartStep.LOAD_RESTORE:
            return self._check_load_restore(now)

        elif self.black_start_step == BlackStartStep.SOC_RECOVERY:
            return self._check_soc_recovery(now)

        return result

    def _check_source_stability(self, now: datetime,
                                 total_renewable_kw: float, diesel_kw: float) -> Dict[str, Any]:
        record = SourceStabilityRecord(
            timestamp=now,
            total_renewable_kw=total_renewable_kw,
            diesel_kw=diesel_kw,
        )
        self.source_stability_records.append(record)

        if len(self.source_stability_records) < 2:
            self.source_stability_consecutive_count = 1
            return {
                "step": self.black_start_step.value,
                "advanced": False,
                "consecutive_count": self.source_stability_consecutive_count,
                "required": self.source_stability_required,
                "reason": "出力记录不足，等待更多数据",
            }

        is_stable = self._evaluate_stability()

        if is_stable:
            self.source_stability_consecutive_count += 1
        else:
            self.source_stability_consecutive_count = 0

        if self.source_stability_consecutive_count >= self.source_stability_required:
            self.black_start_step = BlackStartStep.LOAD_RESTORE
            self.load_restore_total_shed_kw = self._compute_current_total_shed_kw()
            self.load_restore_restored_kw = 0.0

            self.state.add_alert(
                "BLACK_START_STEP_ADVANCED",
                f"黑启动：新能源和柴油机出力已稳定(连续{self.source_stability_required}个周期波动≤{self.source_stability_fluctuation_threshold*100:.0f}%)，开始恢复负荷",
                {"step": "load_restore", "total_shed_kw": self.load_restore_total_shed_kw}
            )

            return {
                "step": self.black_start_step.value,
                "advanced": True,
                "consecutive_count": self.source_stability_consecutive_count,
                "required": self.source_stability_required,
                "reason": f"出力稳定{self.source_stability_required}个周期，进入负荷恢复阶段",
            }

        return {
            "step": self.black_start_step.value,
            "advanced": False,
            "consecutive_count": self.source_stability_consecutive_count,
            "required": self.source_stability_required,
            "reason": f"出力稳定计数{self.source_stability_consecutive_count}/{self.source_stability_required}，未达标",
        }

    def _evaluate_stability(self) -> bool:
        if len(self.source_stability_records) < 2:
            return False

        current = self.source_stability_records[-1]
        previous = self.source_stability_records[-2]

        renewable_fluctuation = 0.0
        if previous.total_renewable_kw > 0:
            renewable_fluctuation = abs(current.total_renewable_kw - previous.total_renewable_kw) / previous.total_renewable_kw
        elif current.total_renewable_kw > 0:
            renewable_fluctuation = 1.0

        diesel_fluctuation = 0.0
        if previous.diesel_kw > 0:
            diesel_fluctuation = abs(current.diesel_kw - previous.diesel_kw) / previous.diesel_kw
        elif current.diesel_kw > 0:
            diesel_fluctuation = 1.0

        threshold = self.source_stability_fluctuation_threshold

        renewable_stable = (renewable_fluctuation <= threshold or
                           (current.total_renewable_kw == 0 and previous.total_renewable_kw == 0))
        diesel_stable = (diesel_fluctuation <= threshold or
                        (current.diesel_kw == 0 and previous.diesel_kw == 0))

        return renewable_stable and diesel_stable

    def _check_load_restore(self, now: datetime) -> Dict[str, Any]:
        current_shed_kw = self._compute_current_total_shed_kw()
        restored_so_far = self.load_restore_total_shed_kw - current_shed_kw
        self.load_restore_restored_kw = restored_so_far

        if current_shed_kw < 0.01:
            bes_id = list(config.BESS_CONFIG.keys())[0]
            soc = self.state.bess_state[bes_id].soc
            if soc >= self.black_start_soc_target:
                self._complete_black_start(now)
                return {
                    "step": BlackStartStep.COMPLETED.value,
                    "advanced": True,
                    "reason": "全部负荷已恢复且SOC达标，黑启动完成",
                }
            else:
                self.black_start_step = BlackStartStep.SOC_RECOVERY
                self.state.add_alert(
                    "BLACK_START_STEP_ADVANCED",
                    f"黑启动：全部负荷已恢复，等待电池SOC恢复至{self.black_start_soc_target*100:.0f}%(当前{soc*100:.1f}%)",
                    {"step": "soc_recovery", "current_soc": soc, "target_soc": self.black_start_soc_target}
                )
                return {
                    "step": self.black_start_step.value,
                    "advanced": True,
                    "reason": f"全部负荷已恢复，SOC {soc*100:.1f}% 未达 {self.black_start_soc_target*100:.0f}%，进入SOC恢复等待",
                }

        max_restore_kw = self.load_restore_total_shed_kw * self.load_restore_max_rate_per_cycle
        actual_restore = min(max_restore_kw, current_shed_kw)

        if actual_restore > 0.01:
            self._perform_load_restore(actual_restore, now)

        remaining = self._compute_current_total_shed_kw()
        progress_pct = 0.0
        if self.load_restore_total_shed_kw > 0:
            progress_pct = (self.load_restore_total_shed_kw - remaining) / self.load_restore_total_shed_kw * 100

        return {
            "step": self.black_start_step.value,
            "advanced": False,
            "restored_kw": round(actual_restore, 2),
            "remaining_shed_kw": round(remaining, 2),
            "total_shed_kw": round(self.load_restore_total_shed_kw, 2),
            "progress_percent": round(progress_pct, 1),
            "reason": f"本周期恢复{actual_restore:.2f}kW，剩余甩负荷{remaining:.2f}kW",
        }

    def _perform_load_restore(self, restore_kw: float, now: datetime):
        restored, leftover = self.state.restore_load_groups_dynamic(
            restore_kw, now, "BLACK_START"
        )
        if restored:
            restore_breakdown = []
            for gid, kw in restored.items():
                gcfg = config.LOAD_GROUP_CONFIG[gid]
                restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
            self.state.add_alert(
                "BLACK_START_LOAD_RESTORED",
                f"黑启动负荷恢复: {', '.join(restore_breakdown)}",
                {"restored": restored, "restore_kw": restore_kw}
            )

    def _check_soc_recovery(self, now: datetime) -> Dict[str, Any]:
        bes_id = list(config.BESS_CONFIG.keys())[0]
        soc = self.state.bess_state[bes_id].soc

        if soc >= self.black_start_soc_target:
            self._complete_black_start(now)
            return {
                "step": BlackStartStep.COMPLETED.value,
                "advanced": True,
                "reason": f"SOC已达{soc*100:.1f}%≥{self.black_start_soc_target*100:.0f}%，黑启动完成",
            }

        return {
            "step": self.black_start_step.value,
            "advanced": False,
            "current_soc": round(soc, 4),
            "target_soc": self.black_start_soc_target,
            "reason": f"SOC {soc*100:.1f}% 未达目标 {self.black_start_soc_target*100:.0f}%，继续等待",
        }

    def _complete_black_start(self, now: datetime):
        self.mode = OperationMode.GRID_CONNECTED
        self.black_start_step = BlackStartStep.COMPLETED

        if self.current_island_event:
            self.current_island_event.exited_at = now
            duration = (now - self.current_island_event.entered_at).total_seconds() / 60.0
            self.current_island_event.duration_minutes = round(duration, 2)
            self.current_island_event.black_start_completed_at = now
            if self.current_island_event.black_start_started_at:
                bs_duration = (now - self.current_island_event.black_start_started_at).total_seconds() / 60.0
                self.current_island_event.black_start_duration_minutes = round(bs_duration, 2)
            self.current_island_event = None

        self.state.add_alert(
            "BLACK_START_COMPLETED",
            "黑启动完成，系统切回并网模式运行",
            {"completed_at": now.isoformat()}
        )

    def _compute_current_total_shed_kw(self) -> float:
        total = 0.0
        for gid, ev in self.state._active_shed_events.items():
            if ev.shed_power_kw > 0.01 and ev.ended_at is None:
                total += ev.shed_power_kw
        return total

    def get_mode_details(self) -> Dict[str, Any]:
        details = {
            "mode": self.mode.value,
            "mode_chinese": self.get_mode_name(),
            "grid_allowed": self.is_grid_allowed(),
            "soc_min": self.get_island_soc_min(),
            "soc_min_chinese": f"{self.get_island_soc_min()*100:.0f}%",
        }

        if self.mode == OperationMode.ISLAND and self.current_island_event:
            duration = 0.0
            if self.current_island_event.entered_at:
                duration = (datetime.now() - self.current_island_event.entered_at).total_seconds() / 60.0
            details["island"] = {
                "event_id": self.current_island_event.event_id,
                "entered_at": self.current_island_event.entered_at.isoformat(),
                "duration_minutes": round(duration, 2),
                "total_shed_kwh": round(self.current_island_event.total_shed_kwh, 4),
                "total_diesel_kwh": round(self.current_island_event.total_diesel_consumption_kwh, 4),
                "total_diesel_liters": round(self.current_island_event.total_diesel_consumption_liters, 2),
                "blackout_occurred": self.current_island_event.blackout_occurred,
            }

        if self.mode == OperationMode.BLACK_START:
            details["black_start"] = self.get_black_start_progress()

        return details

    def get_black_start_progress(self) -> Dict[str, Any]:
        if self.mode != OperationMode.BLACK_START:
            return {"active": False, "message": "当前不在黑启动流程中"}

        current_shed_kw = self._compute_current_total_shed_kw()
        bes_id = list(config.BESS_CONFIG.keys())[0]
        soc = self.state.bess_state[bes_id].soc

        progress = BlackStartProgress(
            current_step=self.black_start_step,
            source_stability_consecutive_count=self.source_stability_consecutive_count,
            source_stability_required=self.source_stability_required,
            source_stability_met=self.source_stability_consecutive_count >= self.source_stability_required,
            load_restore_progress_percent=0.0,
            load_restore_total_shed_kw=self.load_restore_total_shed_kw,
            load_restore_restored_kw=self.load_restore_total_shed_kw - current_shed_kw,
            load_restore_remaining_kw=current_shed_kw,
            soc_current=soc,
            soc_target=self.black_start_soc_target,
            soc_met=soc >= self.black_start_soc_target,
            all_conditions_met=False,
        )

        if progress.load_restore_total_shed_kw > 0:
            progress.load_restore_progress_percent = round(
                progress.load_restore_restored_kw / progress.load_restore_total_shed_kw * 100, 1
            )

        step_cn = {
            BlackStartStep.SOURCE_STABILITY_CHECK: "新能源与柴油机出力稳定性确认",
            BlackStartStep.LOAD_RESTORE: "逐步恢复甩负荷",
            BlackStartStep.SOC_RECOVERY: "电池SOC恢复等待",
            BlackStartStep.COMPLETED: "黑启动完成",
        }

        step_conditions = {
            "source_stability": {
                "met": progress.source_stability_met,
                "current": progress.source_stability_consecutive_count,
                "required": progress.source_stability_required,
                "description": f"连续{progress.source_stability_required}个周期出力波动≤{self.source_stability_fluctuation_threshold*100:.0f}%",
            },
            "load_restore": {
                "met": current_shed_kw < 0.01,
                "total_shed_kw": round(progress.load_restore_total_shed_kw, 2),
                "restored_kw": round(progress.load_restore_restored_kw, 2),
                "remaining_kw": round(progress.load_restore_remaining_kw, 2),
                "progress_percent": progress.load_restore_progress_percent,
                "description": "全部甩负荷恢复",
            },
            "soc_recovery": {
                "met": progress.soc_met,
                "current_soc_percent": round(progress.soc_current * 100, 2),
                "target_soc_percent": round(progress.soc_target * 100, 1),
                "description": f"SOC≥{progress.soc_target*100:.0f}%",
            },
        }

        progress.all_conditions_met = (
            progress.source_stability_met and
            current_shed_kw < 0.01 and
            progress.soc_met
        )

        return {
            "active": True,
            "current_step": self.black_start_step.value,
            "current_step_chinese": step_cn.get(self.black_start_step, "未知"),
            "all_conditions_met": progress.all_conditions_met,
            "conditions": step_conditions,
            "next_action": self._get_next_action_description(progress),
        }

    def _get_next_action_description(self, progress: BlackStartProgress) -> str:
        if self.black_start_step == BlackStartStep.SOURCE_STABILITY_CHECK:
            if progress.source_stability_met:
                return "出力稳定性已确认，即将进入负荷恢复阶段"
            return f"等待出力稳定: {progress.source_stability_consecutive_count}/{progress.source_stability_required}个周期达标"
        elif self.black_start_step == BlackStartStep.LOAD_RESTORE:
            if progress.load_restore_remaining_kw < 0.01:
                return "全部负荷已恢复，检查SOC是否达标"
            return f"逐步恢复负荷: 已恢复{progress.load_restore_progress_percent:.1f}%，剩余{progress.load_restore_remaining_kw:.2f}kW"
        elif self.black_start_step == BlackStartStep.SOC_RECOVERY:
            if progress.soc_met:
                return "SOC已达标，即将完成黑启动"
            return f"等待SOC恢复: 当前{progress.soc_current*100:.1f}%，目标{progress.soc_target*100:.0f}%"
        return "黑启动已完成"

    def get_island_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        events = self.island_events[-limit:]
        result = []
        for ev in reversed(events):
            result.append(self._island_event_to_dict(ev))
        return result

    def _island_event_to_dict(self, ev: IslandEvent) -> Dict[str, Any]:
        return {
            "event_id": ev.event_id,
            "entered_at": ev.entered_at.isoformat(),
            "exited_at": ev.exited_at.isoformat() if ev.exited_at else None,
            "duration_minutes": round(ev.duration_minutes, 2),
            "is_ongoing": ev.exited_at is None,
            "total_shed_kwh": round(ev.total_shed_kwh, 4),
            "total_diesel_consumption_kwh": round(ev.total_diesel_consumption_kwh, 4),
            "total_diesel_consumption_liters": round(ev.total_diesel_consumption_liters, 2),
            "survived": ev.survived,
            "survived_chinese": "成功撑过" if ev.survived else "发生全黑",
            "blackout_occurred": ev.blackout_occurred,
            "blackout_at": ev.blackout_at.isoformat() if ev.blackout_at else None,
            "black_start_started_at": ev.black_start_started_at.isoformat() if ev.black_start_started_at else None,
            "black_start_completed_at": ev.black_start_completed_at.isoformat() if ev.black_start_completed_at else None,
            "black_start_duration_minutes": round(ev.black_start_duration_minutes, 2),
        }

    def get_island_supply_stats(self) -> Dict[str, Any]:
        if self.mode not in (OperationMode.ISLAND, OperationMode.BLACK_START):
            if not self.island_events:
                return {"active": False, "message": "当前非孤岛模式且无历史孤岛事件"}
            latest = self.island_events[-1]
            return {
                "active": False,
                "latest_event": self._island_event_to_dict(latest),
                "total_events": len(self.island_events),
                "total_survived": sum(1 for e in self.island_events if e.survived),
                "total_blackout": sum(1 for e in self.island_events if e.blackout_occurred),
            }

        ev = self.current_island_event
        if ev is None:
            return {"active": True, "message": "孤岛模式运行中但无事件记录"}

        duration = 0.0
        if ev.entered_at:
            duration = (datetime.now() - ev.entered_at).total_seconds() / 60.0

        total_renewable = self.state.get_total_renewable_kw()
        load_kw = self.state.get_load_kw()
        bes_id = list(config.BESS_CONFIG.keys())[0]
        soc = self.state.bess_state[bes_id].soc

        ds_id = list(config.DIESEL_CONFIG.keys())[0]
        diesel_kw = self.state.diesel_state[ds_id].output_kw
        diesel_running = self.state.diesel_state[ds_id].running

        current_shed_kw = self._compute_current_total_shed_kw()
        supply_kw = total_renewable + diesel_kw
        coverage = (supply_kw / load_kw * 100) if load_kw > 0 else 100.0

        return {
            "active": True,
            "mode": self.mode.value,
            "event_id": ev.event_id,
            "duration_minutes": round(duration, 2),
            "power_balance": {
                "load_kw": round(load_kw, 2),
                "renewable_kw": round(total_renewable, 2),
                "diesel_kw": round(diesel_kw, 2),
                "supply_kw": round(supply_kw, 2),
                "shed_kw": round(current_shed_kw, 2),
                "coverage_percent": round(coverage, 1),
            },
            "battery": {
                "soc_percent": round(soc * 100, 2),
                "soc_min_percent": round(self.island_soc_min * 100, 1),
                "soc_above_min": soc > self.island_soc_min,
            },
            "diesel": {
                "running": diesel_running,
                "output_kw": round(diesel_kw, 2),
                "total_consumption_kwh": round(ev.total_diesel_consumption_kwh, 4),
                "total_consumption_liters": round(ev.total_diesel_consumption_liters, 2),
            },
            "cumulative": {
                "total_shed_kwh": round(ev.total_shed_kwh, 4),
                "survived": ev.survived,
                "blackout_occurred": ev.blackout_occurred,
            },
        }
