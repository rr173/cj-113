from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from copy import deepcopy
import uuid

import config
from models import (
    MicrogridState,
    SourceReport,
    LoadReport,
    SimulationScenario,
    SimulationStatus,
    SimulationReport,
    SimulationStepRecord,
    SimulationComparisonReport,
    TimeSeriesSegment,
    SourceTimeSeries,
    LoadTimeSeries,
)
from dispatcher import DispatchEngine


def _get_value_from_segments(segments: List[TimeSeriesSegment], minute: int) -> float:
    for seg in segments:
        if seg.start_minute <= minute < seg.end_minute:
            return seg.value_kw
    return 0.0


def _parse_segments_from_json(segments_json: List[Dict[str, Any]]) -> List[TimeSeriesSegment]:
    return [
        TimeSeriesSegment(
            start_minute=int(seg["start_minute"]),
            end_minute=int(seg["end_minute"]),
            value_kw=float(seg["value_kw"]),
        )
        for seg in segments_json
    ]


def _create_scenario_from_dict(data: Dict[str, Any]) -> SimulationScenario:
    scenario_id = data.get("scenario_id") or f"SCEN-{uuid.uuid4().hex[:8].upper()}"

    pv_series = {}
    for sid, sdata in data.get("pv_series", {}).items():
        pv_series[sid] = SourceTimeSeries(
            source_id=sdata.get("source_id", sid),
            source_type=sdata.get("source_type", "pv"),
            segments=_parse_segments_from_json(sdata.get("segments", [])),
        )

    wt_series = {}
    for sid, sdata in data.get("wt_series", {}).items():
        wt_series[sid] = SourceTimeSeries(
            source_id=sdata.get("source_id", sid),
            source_type=sdata.get("source_type", "wt"),
            segments=_parse_segments_from_json(sdata.get("segments", [])),
        )

    diesel_available = {}
    for ds_id in config.DIESEL_CONFIG:
        diesel_available[ds_id] = data.get("diesel_available", {}).get(ds_id, True)

    load_series_data = data.get("load_series", {})
    load_series = LoadTimeSeries(
        segments=_parse_segments_from_json(load_series_data.get("segments", []))
    )

    initial_soc_override = {}
    for bid, soc in data.get("initial_soc_override", {}).items():
        initial_soc_override[bid] = float(soc)

    return SimulationScenario(
        scenario_id=scenario_id,
        name=data.get("name", "未命名场景"),
        description=data.get("description", ""),
        duration_hours=int(data.get("duration_hours", 24)),
        time_step_minutes=int(data.get("time_step_minutes", 1)),
        pv_series=pv_series,
        wt_series=wt_series,
        diesel_available=diesel_available,
        load_series=load_series,
        initial_soc_override=initial_soc_override,
    )


class SimulationEngine:
    def __init__(self, real_state: MicrogridState):
        self._real_state = real_state
        self._scenarios: Dict[str, SimulationScenario] = {}
        self._simulations: Dict[str, SimulationReport] = {}
        self._counter = 0

    def create_scenario(self, data: Dict[str, Any]) -> SimulationScenario:
        scenario = _create_scenario_from_dict(data)
        self._scenarios[scenario.scenario_id] = scenario
        return scenario

    def list_scenarios(self) -> List[Dict[str, Any]]:
        return [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "description": s.description,
                "duration_hours": s.duration_hours,
                "time_step_minutes": s.time_step_minutes,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat(),
            }
            for s in sorted(self._scenarios.values(), key=lambda x: x.created_at, reverse=True)
        ]

    def get_scenario(self, scenario_id: str) -> Optional[SimulationScenario]:
        return self._scenarios.get(scenario_id)

    def copy_scenario(self, scenario_id: str, new_name: Optional[str] = None) -> Optional[SimulationScenario]:
        src = self._scenarios.get(scenario_id)
        if src is None:
            return None

        new_id = f"SCEN-{uuid.uuid4().hex[:8].upper()}"
        name = new_name or f"{src.name} (副本)"

        new_scenario = SimulationScenario(
            scenario_id=new_id,
            name=name,
            description=src.description,
            duration_hours=src.duration_hours,
            time_step_minutes=src.time_step_minutes,
            pv_series=deepcopy(src.pv_series),
            wt_series=deepcopy(src.wt_series),
            diesel_available=deepcopy(src.diesel_available),
            load_series=deepcopy(src.load_series),
            initial_soc_override=deepcopy(src.initial_soc_override),
        )
        self._scenarios[new_id] = new_scenario
        return new_scenario

    def delete_scenario(self, scenario_id: str) -> bool:
        if scenario_id in self._scenarios:
            del self._scenarios[scenario_id]
            return True
        return False

    def update_scenario(self, scenario_id: str, data: Dict[str, Any]) -> Optional[SimulationScenario]:
        src = self._scenarios.get(scenario_id)
        if src is None:
            return None

        if "name" in data:
            src.name = data["name"]
        if "description" in data:
            src.description = data["description"]
        if "duration_hours" in data:
            src.duration_hours = int(data["duration_hours"])
        if "time_step_minutes" in data:
            src.time_step_minutes = int(data["time_step_minutes"])
        if "pv_series" in data:
            src.pv_series = {}
            for sid, sdata in data["pv_series"].items():
                src.pv_series[sid] = SourceTimeSeries(
                    source_id=sdata.get("source_id", sid),
                    source_type=sdata.get("source_type", "pv"),
                    segments=_parse_segments_from_json(sdata.get("segments", [])),
                )
        if "wt_series" in data:
            src.wt_series = {}
            for sid, sdata in data["wt_series"].items():
                src.wt_series[sid] = SourceTimeSeries(
                    source_id=sdata.get("source_id", sid),
                    source_type=sdata.get("source_type", "wt"),
                    segments=_parse_segments_from_json(sdata.get("segments", [])),
                )
        if "diesel_available" in data:
            src.diesel_available = dict(data["diesel_available"])
        if "load_series" in data:
            src.load_series = LoadTimeSeries(
                segments=_parse_segments_from_json(data["load_series"].get("segments", []))
            )
        if "initial_soc_override" in data:
            src.initial_soc_override = {k: float(v) for k, v in data["initial_soc_override"].items()}

        src.updated_at = datetime.now()
        return src

    def run_simulation(self, scenario_id: str) -> Optional[SimulationReport]:
        scenario = self._scenarios.get(scenario_id)
        if scenario is None:
            return None

        self._counter += 1
        sim_id = f"SIM-{self._counter:06d}"

        report = SimulationReport(
            simulation_id=sim_id,
            scenario_id=scenario.scenario_id,
            scenario_name=scenario.name,
            status=SimulationStatus.RUNNING,
            start_time=datetime.now(),
        )
        self._simulations[sim_id] = report

        try:
            self._execute_simulation(scenario, report)
            report.status = SimulationStatus.COMPLETED
        except Exception as e:
            report.status = SimulationStatus.FAILED
            report.error_message = str(e)

        report.end_time = datetime.now()
        return report

    def _execute_simulation(self, scenario: SimulationScenario, report: SimulationReport):
        total_minutes = scenario.duration_hours * 60
        step_minutes = scenario.time_step_minutes
        total_steps = total_minutes // step_minutes
        report.total_steps = total_steps

        sim_state = self._create_simulation_state(scenario)

        for bes_id in config.BESS_CONFIG:
            report.initial_soc[bes_id] = sim_state.bess_state[bes_id].soc
            report.total_bess_charge_kwh[bes_id] = 0.0
            report.total_bess_discharge_kwh[bes_id] = 0.0
            report.soc_curve[bes_id] = []

        sim_engine = DispatchEngine(sim_state)

        sim_base_time = datetime.now().replace(second=0, microsecond=0)

        cumulative_cost = 0.0
        cumulative_import = 0.0
        cumulative_export = 0.0
        peak_import = 0.0

        for step_idx in range(total_steps):
            scenario_minute = step_idx * step_minutes
            current_sim_time = sim_base_time + timedelta(minutes=scenario_minute)

            self._inject_scenario_data(sim_state, scenario, scenario_minute, current_sim_time)

            decision = sim_engine.execute(now=current_sim_time)

            step_record = SimulationStepRecord(
                step_index=step_idx,
                simulation_time=current_sim_time,
                scenario_minute=scenario_minute,
                pv_output=dict(decision.pv_output),
                wt_output=dict(decision.wt_output),
                diesel_output=dict(decision.diesel_output),
                bess_soc_before={bid: ba["soc_before"] for bid, ba in decision.bess_action.items()},
                bess_soc_after={bid: ba["soc_after"] for bid, ba in decision.bess_action.items()},
                bess_charge_kw={bid: ba["charge_kw"] for bid, ba in decision.bess_action.items()},
                bess_discharge_kw={bid: ba["discharge_kw"] for bid, ba in decision.bess_action.items()},
                grid_import_kw=decision.grid_import_kw,
                grid_export_kw=decision.grid_export_kw,
                load_served_kw=decision.load_served_kw,
                load_shed_kw=decision.load_shed_kw,
                step_cost=decision.cost,
                tariff_period=decision.tariff_period,
                notes=list(decision.notes),
            )
            report.step_records.append(step_record)

            cumulative_cost += decision.cost
            time_interval_h = step_minutes / 60.0
            import_kwh = decision.grid_import_kw * time_interval_h
            export_kwh = decision.grid_export_kw * time_interval_h
            cumulative_import += import_kwh
            cumulative_export += export_kwh
            if decision.tariff_period == "peak":
                peak_import += import_kwh

            for bes_id in config.BESS_CONFIG:
                report.total_bess_charge_kwh[bes_id] += decision.bess_action[bes_id]["charge_kw"] * time_interval_h
                report.total_bess_discharge_kwh[bes_id] += decision.bess_action[bes_id]["discharge_kw"] * time_interval_h
                report.soc_curve[bes_id].append({
                    "minute": scenario_minute,
                    "time": current_sim_time.isoformat(),
                    "soc_percent": round(decision.bess_action[bes_id]["soc_after"] * 100, 2),
                })

            report.cost_curve.append({
                "minute": scenario_minute,
                "time": current_sim_time.isoformat(),
                "step_cost": round(decision.cost, 4),
                "cumulative_cost": round(cumulative_cost, 4),
                "tariff_period": decision.tariff_period,
            })

            report.completed_steps = step_idx + 1

        report.total_cost = cumulative_cost
        report.total_grid_import_kwh = cumulative_import
        report.total_grid_export_kwh = cumulative_export
        report.peak_grid_import_kwh = peak_import

        for ds_id in config.DIESEL_CONFIG:
            report.total_diesel_generated_kwh += sim_state.diesel_state[ds_id].total_generated_kwh
            report.total_diesel_starts += sim_state.diesel_state[ds_id].total_starts

        report.total_load_shed_kwh = sim_state.stats.total_load_shed_kwh

        for bes_id in config.BESS_CONFIG:
            report.final_soc[bes_id] = sim_state.bess_state[bes_id].soc

    def _create_simulation_state(self, scenario: SimulationScenario) -> MicrogridState:
        sim_state = MicrogridState()

        for bes_id, cfg in config.BESS_CONFIG.items():
            if bes_id in scenario.initial_soc_override:
                sim_state.bess_state[bes_id].soc = scenario.initial_soc_override[bes_id]
            else:
                real_soc = self._real_state.bess_state.get(bes_id)
                if real_soc is not None:
                    sim_state.bess_state[bes_id].soc = real_soc.soc
                else:
                    sim_state.bess_state[bes_id].soc = cfg["initial_soc"]

        return sim_state

    def _inject_scenario_data(self, state: MicrogridState, scenario: SimulationScenario,
                               scenario_minute: int, timestamp: datetime):
        for pv_id in config.PV_CONFIG:
            pv_series = scenario.pv_series.get(pv_id)
            if pv_series and pv_series.segments:
                value = _get_value_from_segments(pv_series.segments, scenario_minute)
                report = SourceReport(
                    source_id=pv_id,
                    source_type="pv",
                    power_kw=value,
                    available=True,
                    timestamp=timestamp,
                )
            else:
                report = SourceReport(
                    source_id=pv_id,
                    source_type="pv",
                    power_kw=0.0,
                    available=False,
                    timestamp=timestamp,
                )
            state.report_source(report)

        for wt_id in config.WT_CONFIG:
            wt_series = scenario.wt_series.get(wt_id)
            if wt_series and wt_series.segments:
                value = _get_value_from_segments(wt_series.segments, scenario_minute)
                report = SourceReport(
                    source_id=wt_id,
                    source_type="wt",
                    power_kw=value,
                    available=True,
                    timestamp=timestamp,
                )
            else:
                report = SourceReport(
                    source_id=wt_id,
                    source_type="wt",
                    power_kw=0.0,
                    available=False,
                    timestamp=timestamp,
                )
            state.report_source(report)

        for ds_id in config.DIESEL_CONFIG:
            available = scenario.diesel_available.get(ds_id, True)
            report = SourceReport(
                source_id=ds_id,
                source_type="diesel",
                power_kw=0.0,
                available=available,
                timestamp=timestamp,
            )
            state.report_source(report)

        load_value = _get_value_from_segments(scenario.load_series.segments, scenario_minute)
        load_report = LoadReport(load_kw=load_value, timestamp=timestamp)
        state.report_load(load_report)

    def get_simulation(self, sim_id: str) -> Optional[SimulationReport]:
        return self._simulations.get(sim_id)

    def list_simulations(self, scenario_id: Optional[str] = None) -> List[Dict[str, Any]]:
        results = []
        for sim in self._simulations.values():
            if scenario_id and sim.scenario_id != scenario_id:
                continue
            results.append({
                "simulation_id": sim.simulation_id,
                "scenario_id": sim.scenario_id,
                "scenario_name": sim.scenario_name,
                "status": sim.status,
                "total_steps": sim.total_steps,
                "completed_steps": sim.completed_steps,
                "start_time": sim.start_time.isoformat() if sim.start_time else None,
                "end_time": sim.end_time.isoformat() if sim.end_time else None,
            })
        return sorted(results, key=lambda x: x["start_time"] or "", reverse=True)

    def compare_simulations(self, sim_a_id: str, sim_b_id: str) -> Optional[SimulationComparisonReport]:
        sim_a = self._simulations.get(sim_a_id)
        sim_b = self._simulations.get(sim_b_id)

        if sim_a is None or sim_b is None:
            return None

        if sim_a.status != SimulationStatus.COMPLETED or sim_b.status != SimulationStatus.COMPLETED:
            return None

        comparison = SimulationComparisonReport(
            simulation_a_id=sim_a_id,
            simulation_b_id=sim_b_id,
            scenario_a_name=sim_a.scenario_name,
            scenario_b_name=sim_b.scenario_name,
            cost_diff=sim_a.total_cost - sim_b.total_cost,
            grid_import_diff=sim_a.total_grid_import_kwh - sim_b.total_grid_import_kwh,
            grid_export_diff=sim_a.total_grid_export_kwh - sim_b.total_grid_export_kwh,
            diesel_starts_diff=sim_a.total_diesel_starts - sim_b.total_diesel_starts,
            diesel_generated_diff=sim_a.total_diesel_generated_kwh - sim_b.total_diesel_generated_kwh,
            load_shed_diff=sim_a.total_load_shed_kwh - sim_b.total_load_shed_kwh,
        )

        all_bes_ids = set(list(sim_a.total_bess_charge_kwh.keys()) + list(sim_b.total_bess_charge_kwh.keys()))
        for bes_id in all_bes_ids:
            a_charge = sim_a.total_bess_charge_kwh.get(bes_id, 0.0)
            b_charge = sim_b.total_bess_charge_kwh.get(bes_id, 0.0)
            a_discharge = sim_a.total_bess_discharge_kwh.get(bes_id, 0.0)
            b_discharge = sim_b.total_bess_discharge_kwh.get(bes_id, 0.0)
            a_soc_final = sim_a.final_soc.get(bes_id, 0.0)
            b_soc_final = sim_b.final_soc.get(bes_id, 0.0)
            capacity = config.BESS_CONFIG.get(bes_id, {}).get("capacity_kwh", 500.0)

            comparison.bess_charge_diff[bes_id] = a_charge - b_charge
            comparison.bess_discharge_diff[bes_id] = a_discharge - b_discharge
            comparison.bess_cycles_diff[bes_id] = (a_discharge - b_discharge) / capacity
            comparison.final_soc_diff[bes_id] = a_soc_final - b_soc_final

        return comparison
