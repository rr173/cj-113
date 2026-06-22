from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import asdict
import uuid

import config
from models import (
    DayAheadForecast,
    DayAheadForecastHour,
    HourlyDispatchPlan,
    DayAheadPlan,
    HourlyActualData,
    CorrectionEvent,
    PlanEvaluationReport,
)


class DayAheadDispatchEngine:
    DEVIATION_THRESHOLD = 0.2

    def __init__(self, microgrid_state=None):
        self.state = microgrid_state
        self._forecast_counter = 0
        self._plan_counter = 0
        self._correction_counter = 0
        self._report_counter = 0

    def _generate_forecast_id(self) -> str:
        self._forecast_counter += 1
        return f"FORECAST-{self._forecast_counter:06d}"

    def _generate_plan_id(self) -> str:
        self._plan_counter += 1
        return f"DAYPLAN-{self._plan_counter:06d}"

    def _generate_correction_id(self) -> str:
        self._correction_counter += 1
        return f"CORRECT-{self._correction_counter:06d}"

    def _generate_report_id(self) -> str:
        self._report_counter += 1
        return f"EVAL-{self._report_counter:06d}"

    def _get_bess_config(self):
        bes_id = list(config.BESS_CONFIG.keys())[0]
        return config.BESS_CONFIG[bes_id]

    def _get_diesel_config(self):
        ds_id = list(config.DIESEL_CONFIG.keys())[0]
        return config.DIESEL_CONFIG[ds_id]

    def _get_grid_price_for_hour(self, hour: int) -> Tuple[str, float]:
        period = config.get_tariff_period(hour)
        price = config.GRID_TARIFF[period]["price"]
        return period, price

    def _compute_hour_cost(
        self,
        grid_import_kw: float,
        diesel_kw: float,
        diesel_startup: bool,
        hour: int,
    ) -> float:
        period, grid_price = self._get_grid_price_for_hour(hour)
        diesel_cfg = self._get_diesel_config()

        grid_cost = grid_import_kw * grid_price
        diesel_gen_cost = diesel_kw * diesel_cfg["generation_cost"]
        diesel_startup_cost = diesel_cfg["startup_cost"] if diesel_startup else 0.0

        return grid_cost + diesel_gen_cost + diesel_startup_cost

    def _update_soc(
        self,
        soc_start: float,
        charge_kw: float,
        discharge_kw: float,
        time_interval_hours: float = 1.0,
    ) -> float:
        cfg = self._get_bess_config()
        capacity = cfg["capacity_kwh"]
        charge_eff = cfg["charge_efficiency"]
        discharge_eff = cfg["discharge_efficiency"]
        soc_min = cfg["soc_min"]
        soc_max = cfg["soc_max"]

        if charge_kw > 0:
            energy_in = charge_kw * time_interval_hours * charge_eff
            soc = soc_start + energy_in / capacity
        elif discharge_kw > 0:
            energy_out = discharge_kw * time_interval_hours / discharge_eff
            soc = soc_start - energy_out / capacity
        else:
            soc = soc_start

        return max(soc_min, min(soc_max, soc))

    def _get_future_max_price(self, start_hour: int, end_hour: int = 24) -> float:
        max_price = 0.0
        for h in range(start_hour, end_hour):
            _, price = self._get_grid_price_for_hour(h)
            if price > max_price:
                max_price = price
        return max_price

    def _get_past_min_price(self, start_hour: int, end_hour: int = 0) -> float:
        min_price = float('inf')
        for h in range(end_hour, start_hour):
            _, price = self._get_grid_price_for_hour(h)
            if price < min_price:
                min_price = price
        return min_price

    def _optimize_hour_dispatch(
        self,
        hour: int,
        forecast_load: float,
        forecast_pv: float,
        forecast_wt: float,
        soc_start: float,
        prev_diesel_running: bool,
        diesel_consecutive_hours: int,
        future_max_price: Optional[float] = None,
    ) -> Tuple[HourlyDispatchPlan, bool, int]:
        bess_cfg = self._get_bess_config()
        diesel_cfg = self._get_diesel_config()
        min_runtime_hours = diesel_cfg["min_runtime_minutes"] / 60.0

        period, grid_price = self._get_grid_price_for_hour(hour)
        total_renewable = forecast_pv + forecast_wt
        net_load = max(0.0, forecast_load - total_renewable)

        max_charge = bess_cfg["max_charge_power"]
        max_discharge = bess_cfg["max_discharge_power"]
        soc_min = bess_cfg["soc_min"]
        soc_max = bess_cfg["soc_max"]
        capacity = bess_cfg["capacity_kwh"]

        diesel_rated = diesel_cfg["rated_power"]
        diesel_gen_cost = diesel_cfg["generation_cost"]
        diesel_startup_cost = diesel_cfg["startup_cost"]

        planned_charge = 0.0
        planned_discharge = 0.0
        planned_grid_import = 0.0
        planned_diesel = 0.0
        diesel_running = prev_diesel_running
        diesel_startup = False
        notes = []

        is_valley = period == "valley"
        is_peak = period == "peak"

        if future_max_price is None:
            future_max_price = self._get_future_max_price(hour + 1)

        has_higher_price_future = future_max_price > grid_price
        has_much_higher_price_future = future_max_price > grid_price * 1.2

        if is_valley:
            if has_higher_price_future:
                notes.append("谷时段：后面有高价时段，尽量充满电池")
            else:
                notes.append("谷时段：优先利用低价电充电")
            charge_energy_needed = (soc_max - soc_start) * capacity
            max_charge_by_soc = charge_energy_needed / bess_cfg["charge_efficiency"]
            target_charge = min(max_charge, max_charge_by_soc)

            if target_charge > 0.01:
                planned_charge = target_charge
                surplus_renewable = max(0.0, total_renewable - forecast_load)
                charge_from_renewable = min(surplus_renewable, planned_charge)
                charge_from_grid = planned_charge - charge_from_renewable

                if charge_from_renewable > 0:
                    notes.append(f"利用新能源充电 {charge_from_renewable:.1f}kW")
                if charge_from_grid > 0:
                    notes.append(f"谷价购电充电 {charge_from_grid:.1f}kW")

                planned_grid_import = max(0.0, forecast_load - total_renewable + charge_from_grid)
            else:
                notes.append("电池已满，仅满足负荷需求")
                planned_grid_import = max(0.0, forecast_load - total_renewable)

        elif is_peak:
            notes.append("峰时段：优先放电减少购电")
            discharge_energy_available = (soc_start - soc_min) * capacity * bess_cfg["discharge_efficiency"]
            max_discharge_by_soc = discharge_energy_available

            target_discharge = min(max_discharge, max_discharge_by_soc, net_load)
            if target_discharge > 0.01:
                planned_discharge = target_discharge
                net_load_after_discharge = net_load - planned_discharge
                notes.append(f"电池放电 {planned_discharge:.1f}kW，减少峰时购电")
            else:
                net_load_after_discharge = net_load
                notes.append("电池SOC低，无法放电")

            remaining_load = net_load_after_discharge

            if remaining_load > 0.01:
                if diesel_gen_cost < grid_price:
                    if prev_diesel_running:
                        diesel_needed = min(diesel_rated, remaining_load)
                        planned_diesel = diesel_needed
                        remaining_load -= diesel_needed
                        notes.append(f"柴油机发电 {planned_diesel:.1f}kW（比电网便宜）")
                    elif remaining_load * grid_price > diesel_startup_cost:
                        planned_diesel = min(diesel_rated, remaining_load)
                        diesel_startup = True
                        diesel_running = True
                        remaining_load -= planned_diesel
                        notes.append(f"启动柴油机发电 {planned_diesel:.1f}kW，节省峰时电费")

                if remaining_load > 0.01:
                    planned_grid_import = remaining_load
                    notes.append(f"峰时购电 {planned_grid_import:.1f}kW")

        else:
            if has_much_higher_price_future:
                notes.append("平时段：后面有峰时段，保留电量不放电")
                if total_renewable >= forecast_load:
                    surplus = total_renewable - forecast_load
                    if soc_start < soc_max:
                        charge_energy_needed = (soc_max - soc_start) * capacity
                        max_charge_by_soc = charge_energy_needed / bess_cfg["charge_efficiency"]
                        planned_charge = min(max_charge, max_charge_by_soc, surplus)
                        if planned_charge > 0.01:
                            notes.append(f"利用新能源盈余充电 {planned_charge:.1f}kW，为峰时段储备")
                else:
                    net_load = forecast_load - total_renewable
                    planned_grid_import = net_load
                    notes.append(f"购电 {planned_grid_import:.1f}kW，保留电池电量")
            else:
                notes.append("平时段：后面无更高价时段，平衡充放电")
                if total_renewable >= forecast_load:
                    surplus = total_renewable - forecast_load
                    if soc_start < soc_max:
                        charge_energy_needed = (soc_max - soc_start) * capacity
                        max_charge_by_soc = charge_energy_needed / bess_cfg["charge_efficiency"]
                        planned_charge = min(max_charge, max_charge_by_soc, surplus)
                        if planned_charge > 0.01:
                            notes.append(f"利用新能源盈余充电 {planned_charge:.1f}kW")
                else:
                    net_load = forecast_load - total_renewable
                    if soc_start > soc_min + 0.1:
                        discharge_energy_available = (soc_start - soc_min) * capacity * bess_cfg["discharge_efficiency"]
                        max_discharge_by_soc = discharge_energy_available
                        planned_discharge = min(max_discharge, max_discharge_by_soc, net_load)
                        if planned_discharge > 0.01:
                            net_load -= planned_discharge
                            notes.append(f"电池放电 {planned_discharge:.1f}kW")

                    if net_load > 0.01:
                        if prev_diesel_running and diesel_consecutive_hours < min_runtime_hours:
                            diesel_needed = min(diesel_rated, net_load)
                            planned_diesel = diesel_needed
                            net_load -= diesel_needed
                            notes.append(f"柴油机继续运行发电 {planned_diesel:.1f}kW（满足最小运行时间）")
                        elif diesel_gen_cost < grid_price and net_load * grid_price > diesel_startup_cost:
                            planned_diesel = min(diesel_rated, net_load)
                            diesel_startup = True
                            diesel_running = True
                            net_load -= planned_diesel
                            notes.append(f"启动柴油机发电 {planned_diesel:.1f}kW")

                        if net_load > 0.01:
                            planned_grid_import = net_load
                            notes.append(f"购电 {planned_grid_import:.1f}kW")

        if diesel_running and not prev_diesel_running:
            diesel_consecutive_hours = 1
        elif diesel_running and prev_diesel_running:
            diesel_consecutive_hours += 1
        else:
            diesel_consecutive_hours = 0

        if prev_diesel_running and not diesel_running and diesel_consecutive_hours < min_runtime_hours:
            notes.append("柴油机强制继续运行（不满足最小停机时间）")
            diesel_running = True
            planned_diesel = min(diesel_rated, net_load if net_load > 0 else 0)
            if planned_diesel > 0:
                planned_grid_import = max(0.0, net_load - planned_diesel)
            diesel_consecutive_hours += 1

        soc_end = self._update_soc(soc_start, planned_charge, planned_discharge)

        hour_cost = self._compute_hour_cost(
            planned_grid_import, planned_diesel, diesel_startup, hour
        )

        if diesel_startup:
            notes.append(f"柴油机启动成本 {diesel_startup_cost:.2f}元")

        plan_hour = HourlyDispatchPlan(
            hour=hour,
            forecast_load_kw=forecast_load,
            forecast_pv_kw=forecast_pv,
            forecast_wt_kw=forecast_wt,
            tariff_period=period,
            grid_buy_price=grid_price,
            planned_charge_kw=round(planned_charge, 4),
            planned_discharge_kw=round(planned_discharge, 4),
            planned_grid_import_kw=round(planned_grid_import, 4),
            planned_diesel_kw=round(planned_diesel, 4),
            planned_cost=round(hour_cost, 4),
            soc_start=round(soc_start, 4),
            soc_end=round(soc_end, 4),
            diesel_running=diesel_running,
            notes=notes,
        )

        return plan_hour, diesel_running, diesel_consecutive_hours

    def generate_plan(
        self,
        forecast: DayAheadForecast,
        initial_soc: Optional[float] = None,
        start_hour: int = 0,
        end_hour: int = 24,
        existing_hours: Optional[Dict[int, HourlyDispatchPlan]] = None,
    ) -> DayAheadPlan:
        if len(forecast.hours) != 24:
            raise ValueError(f"预测数据必须包含24小时，当前为{len(forecast.hours)}小时")

        bess_cfg = self._get_bess_config()
        soc_min = bess_cfg["soc_min"]
        soc_max = bess_cfg["soc_max"]

        if initial_soc is None:
            initial_soc = forecast.initial_soc
        if not (soc_min <= initial_soc <= soc_max):
            raise ValueError(f"初始SOC必须在 [{soc_min}, {soc_max}] 区间内，当前为 {initial_soc}")

        plan_id = self._generate_plan_id()
        plan_date = forecast.forecast_date
        generated_at = datetime.now()

        hours: Dict[int, HourlyDispatchPlan] = {}
        total_cost = 0.0

        current_soc = initial_soc
        diesel_running = False
        diesel_consecutive_hours = 0

        if existing_hours:
            for h in range(start_hour):
                if h in existing_hours:
                    hours[h] = existing_hours[h]
                    diesel_running = existing_hours[h].diesel_running
                    if diesel_running:
                        diesel_consecutive_hours += 1
                    else:
                        diesel_consecutive_hours = 0
                    total_cost += existing_hours[h].planned_cost

        for hour in range(start_hour, end_hour):
            forecast_hour = None
            for fh in forecast.hours:
                if fh.hour == hour:
                    forecast_hour = fh
                    break
            if forecast_hour is None:
                raise ValueError(f"缺少小时 {hour} 的预测数据")

            future_max_price = self._get_future_max_price(hour + 1, end_hour)

            plan_hour, diesel_running, diesel_consecutive_hours = self._optimize_hour_dispatch(
                hour=hour,
                forecast_load=forecast_hour.forecast_load_kw,
                forecast_pv=forecast_hour.forecast_pv_kw,
                forecast_wt=forecast_hour.forecast_wt_kw,
                soc_start=current_soc,
                prev_diesel_running=diesel_running,
                diesel_consecutive_hours=diesel_consecutive_hours,
                future_max_price=future_max_price,
            )

            hours[hour] = plan_hour
            current_soc = plan_hour.soc_end
            total_cost += plan_hour.planned_cost

        plan = DayAheadPlan(
            plan_id=plan_id,
            plan_date=plan_date,
            generated_at=generated_at,
            forecast_id=forecast.forecast_id,
            initial_soc=initial_soc,
            hours=hours,
            total_planned_cost=round(total_cost, 4),
            generated_by="auto",
        )

        return plan

    def check_deviation_and_correct(
        self,
        current_plan: DayAheadPlan,
        actual_data: HourlyActualData,
        forecast: DayAheadForecast,
    ) -> Tuple[Optional[CorrectionEvent], Optional[DayAheadPlan]]:
        hour = actual_data.hour
        plan_hour = current_plan.hours.get(hour)
        if plan_hour is None:
            return None, None

        load_deviation = abs(actual_data.actual_load_kw - plan_hour.forecast_load_kw) / max(plan_hour.forecast_load_kw, 0.01)
        pv_deviation = abs(actual_data.actual_pv_kw - plan_hour.forecast_pv_kw) / max(plan_hour.forecast_pv_kw, 0.01)
        wt_deviation = abs(actual_data.actual_wt_kw - plan_hour.forecast_wt_kw) / max(plan_hour.forecast_wt_kw, 0.01)
        total_renewable_forecast = plan_hour.forecast_pv_kw + plan_hour.forecast_wt_kw
        total_renewable_actual = actual_data.actual_pv_kw + actual_data.actual_wt_kw
        renewable_deviation = abs(total_renewable_actual - total_renewable_forecast) / max(total_renewable_forecast, 0.01)

        should_correct = False
        trigger_reason = ""
        deviation_details = {
            "hour": hour,
            "load_forecast": plan_hour.forecast_load_kw,
            "load_actual": actual_data.actual_load_kw,
            "load_deviation_percent": round(load_deviation * 100, 2),
            "pv_forecast": plan_hour.forecast_pv_kw,
            "pv_actual": actual_data.actual_pv_kw,
            "pv_deviation_percent": round(pv_deviation * 100, 2),
            "wt_forecast": plan_hour.forecast_wt_kw,
            "wt_actual": actual_data.actual_wt_kw,
            "wt_deviation_percent": round(wt_deviation * 100, 2),
            "renewable_forecast": total_renewable_forecast,
            "renewable_actual": total_renewable_actual,
            "renewable_deviation_percent": round(renewable_deviation * 100, 2),
        }

        if actual_data.actual_load_kw > plan_hour.forecast_load_kw * (1 + self.DEVIATION_THRESHOLD):
            should_correct = True
            trigger_reason = f"实际负荷({actual_data.actual_load_kw:.1f}kW)比预测({plan_hour.forecast_load_kw:.1f}kW)高出{load_deviation*100:.1f}%"

        if total_renewable_actual < total_renewable_forecast * (1 - self.DEVIATION_THRESHOLD):
            should_correct = True
            if trigger_reason:
                trigger_reason += "；"
            trigger_reason += f"实际新能源({total_renewable_actual:.1f}kW)比预测({total_renewable_forecast:.1f}kW)低{renewable_deviation*100:.1f}%"

        if not should_correct:
            return None, None

        next_hour = hour + 1
        if next_hour >= 24:
            return None, None

        new_soc = actual_data.soc_end
        corrected_hours = list(range(next_hour, 24))

        new_plan = self.generate_plan(
            forecast=forecast,
            initial_soc=new_soc,
            start_hour=next_hour,
            end_hour=24,
            existing_hours=current_plan.hours,
        )
        new_plan.version = current_plan.version + 1
        new_plan.parent_plan_id = current_plan.plan_id
        new_plan.note = f"滚动校正：时段{hour}触发，基于实际SOC={new_soc:.3f}重算时段{next_hour}-23"
        new_plan.is_active = True

        correction_event = CorrectionEvent(
            event_id=self._generate_correction_id(),
            plan_id=current_plan.plan_id,
            triggered_at=datetime.now(),
            triggered_by_hour=hour,
            trigger_reason=trigger_reason,
            deviation_details=deviation_details,
            old_plan_id=current_plan.plan_id,
            new_plan_id=new_plan.plan_id,
            corrected_hours=corrected_hours,
        )

        return correction_event, new_plan

    def record_actual_data(
        self,
        plan: DayAheadPlan,
        actual_data: HourlyActualData,
    ) -> bool:
        plan_hour = plan.hours.get(actual_data.hour)
        if plan_hour is None:
            return False

        if actual_data.timestamp is None:
            actual_data.timestamp = datetime.now()

        plan_hour.actual_data = asdict(actual_data)

        load_dev = abs(actual_data.actual_load_kw - plan_hour.forecast_load_kw) / max(plan_hour.forecast_load_kw, 0.01)
        pv_dev = abs(actual_data.actual_pv_kw - plan_hour.forecast_pv_kw) / max(plan_hour.forecast_pv_kw, 0.01)
        wt_dev = abs(actual_data.actual_wt_kw - plan_hour.forecast_wt_kw) / max(plan_hour.forecast_wt_kw, 0.01)
        charge_dev = abs(actual_data.actual_charge_kw - plan_hour.planned_charge_kw)
        discharge_dev = abs(actual_data.actual_discharge_kw - plan_hour.planned_discharge_kw)
        grid_dev = abs(actual_data.actual_grid_import_kw - plan_hour.planned_grid_import_kw)
        diesel_dev = abs(actual_data.actual_diesel_kw - plan_hour.planned_diesel_kw)
        cost_dev = abs(actual_data.actual_cost - plan_hour.planned_cost)
        soc_dev = abs(actual_data.soc_end - plan_hour.soc_end)

        plan_hour.deviation = {
            "load_deviation_percent": round(load_dev * 100, 2),
            "pv_deviation_percent": round(pv_dev * 100, 2),
            "wt_deviation_percent": round(wt_dev * 100, 2),
            "charge_deviation_kw": round(charge_dev, 4),
            "discharge_deviation_kw": round(discharge_dev, 4),
            "grid_import_deviation_kw": round(grid_dev, 4),
            "diesel_deviation_kw": round(diesel_dev, 4),
            "cost_deviation": round(cost_dev, 4),
            "soc_deviation": round(soc_dev, 4),
        }

        return True

    def evaluate_plan(
        self,
        plan: DayAheadPlan,
        correction_events: List[CorrectionEvent],
    ) -> PlanEvaluationReport:
        actual_hours = [h for h in plan.hours.values() if h.actual_data is not None]
        total_actual_cost = sum(h.actual_data["actual_cost"] for h in actual_hours)

        load_errors = []
        hourly_deviations = []
        for h in plan.hours.values():
            if h.deviation:
                load_errors.append(h.deviation["load_deviation_percent"])
                hourly_deviations.append({
                    "hour": h.hour,
                    "load_deviation_percent": h.deviation["load_deviation_percent"],
                    "pv_deviation_percent": h.deviation["pv_deviation_percent"],
                    "wt_deviation_percent": h.deviation["wt_deviation_percent"],
                    "cost_deviation": h.deviation["cost_deviation"],
                    "soc_deviation": h.deviation["soc_deviation"],
                    "planned_cost": h.planned_cost,
                    "actual_cost": h.actual_data["actual_cost"] if h.actual_data else None,
                })

        avg_load_error = sum(load_errors) / len(load_errors) if load_errors else 0.0
        cost_deviation_percent = abs(total_actual_cost - plan.total_planned_cost) / max(plan.total_planned_cost, 0.01) * 100

        total_charged = sum(h.planned_charge_kw for h in plan.hours.values())
        total_discharged = sum(h.planned_discharge_kw for h in plan.hours.values())
        total_grid_import = sum(h.planned_grid_import_kw for h in plan.hours.values())
        total_diesel_gen = sum(h.planned_diesel_kw for h in plan.hours.values())

        valley_hours = [h for h in plan.hours.values() if h.tariff_period == "valley"]
        peak_hours = [h for h in plan.hours.values() if h.tariff_period == "peak"]

        report = PlanEvaluationReport(
            report_id=self._generate_report_id(),
            plan_id=plan.plan_id,
            plan_date=plan.plan_date,
            generated_at=datetime.now(),
            total_planned_cost=round(plan.total_planned_cost, 4),
            total_actual_cost=round(total_actual_cost, 4),
            cost_deviation_percent=round(cost_deviation_percent, 2),
            avg_load_forecast_error_percent=round(avg_load_error, 2),
            correction_count=len(correction_events),
            correction_events=[e.event_id for e in correction_events],
            hourly_deviations=hourly_deviations,
            details={
                "total_planned_charge_kwh": round(total_charged, 4),
                "total_planned_discharge_kwh": round(total_discharged, 4),
                "total_planned_grid_import_kwh": round(total_grid_import, 4),
                "total_planned_diesel_kwh": round(total_diesel_gen, 4),
                "valley_hour_count": len(valley_hours),
                "peak_hour_count": len(peak_hours),
                "valley_average_grid_import": round(sum(h.planned_grid_import_kw for h in valley_hours) / max(len(valley_hours), 1), 4),
                "peak_average_grid_import": round(sum(h.planned_grid_import_kw for h in peak_hours) / max(len(peak_hours), 1), 4),
                "actual_hours_with_data": len(actual_hours),
                "initial_soc": plan.initial_soc,
                "final_soc": actual_hours[-1].actual_data["soc_end"] if actual_hours else None,
            },
        )

        return report

    def validate_forecast(self, forecast_data: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if len(forecast_data) != 24:
            return False, f"必须提供24小时的预测数据，当前为{len(forecast_data)}小时"

        hours_seen = set()
        for i, hour_data in enumerate(forecast_data):
            hour = hour_data.get("hour")
            if hour is None:
                hour = i
            if hour in hours_seen:
                return False, f"小时 {hour} 重复出现"
            if not (0 <= hour <= 23):
                return False, f"小时必须在0-23之间，当前为 {hour}"
            hours_seen.add(hour)

            required_fields = ["forecast_load_kw", "forecast_pv_kw", "forecast_wt_kw"]
            for field in required_fields:
                if field not in hour_data:
                    return False, f"小时 {hour} 缺少必填字段: {field}"
                try:
                    val = float(hour_data[field])
                    if val < 0:
                        return False, f"小时 {hour} 的 {field} 不能为负数"
                except (TypeError, ValueError):
                    return False, f"小时 {hour} 的 {field} 必须是数值"

        return True, "预测数据有效"

    def create_forecast(
        self,
        forecast_date: str,
        hours_data: List[Dict[str, Any]],
        initial_soc: Optional[float] = None,
    ) -> DayAheadForecast:
        valid, error = self.validate_forecast(hours_data)
        if not valid:
            raise ValueError(error)

        if initial_soc is None:
            initial_soc = self._get_bess_config()["initial_soc"]

        forecast_hours = []
        for hour_data in hours_data:
            hour = hour_data.get("hour")
            if hour is None:
                hour = len(forecast_hours)
            fh = DayAheadForecastHour(
                hour=hour,
                forecast_load_kw=float(hour_data["forecast_load_kw"]),
                forecast_pv_kw=float(hour_data["forecast_pv_kw"]),
                forecast_wt_kw=float(hour_data["forecast_wt_kw"]),
            )
            forecast_hours.append(fh)

        forecast_hours.sort(key=lambda x: x.hour)

        forecast = DayAheadForecast(
            forecast_id=self._generate_forecast_id(),
            forecast_date=forecast_date,
            submitted_at=datetime.now(),
            hours=forecast_hours,
            initial_soc=initial_soc,
            status="pending",
        )

        return forecast
