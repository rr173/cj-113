from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import asdict
from copy import deepcopy

import config
from models import (
    MicrogridState,
    DispatchDecision,
    ArbitrageHourlyRecord,
    ArbitrageInterruptionRecord,
    ArbitrageSettlementDetail,
    ArbitrageSettlementSummary,
    ArbitrageTrendPoint,
    ArbitrageAlert,
)


class ArbitrageAnalyzer:
    def __init__(self, state: MicrogridState):
        self.state = state
        self.enabled = config.ARBITRAGE_ANALYSIS_CONFIG.get("enable_arbitrage_analysis", True)
        self.settlement_hour = config.ARBITRAGE_ANALYSIS_CONFIG.get("settlement_hour", 0)
        self.settlement_minute = config.ARBITRAGE_ANALYSIS_CONFIG.get("settlement_minute", 0)
        self.low_execution_rate_threshold = config.ARBITRAGE_ANALYSIS_CONFIG.get("low_execution_rate_threshold", 50.0)
        self.consecutive_low_days_threshold = config.ARBITRAGE_ANALYSIS_CONFIG.get("consecutive_low_days_threshold", 3)
        self.hourly_records: List[ArbitrageHourlyRecord] = []
        self.settlements: Dict[str, ArbitrageSettlementDetail] = {}
        self.alerts: List[ArbitrageAlert] = []
        self._settlement_counter: int = 0
        self._interruption_counter: int = 0
        self._alert_counter: int = 0
        self._hour_counter: int = 0
        self._last_settlement_date: Optional[str] = None

    def _generate_settlement_id(self) -> str:
        self._settlement_counter += 1
        return f"ARB-SETT-{self._settlement_counter:08d}"

    def _generate_interruption_id(self) -> str:
        self._interruption_counter += 1
        return f"ARB-INT-{self._interruption_counter:08d}"

    def _generate_alert_id(self) -> str:
        self._alert_counter += 1
        return f"ARB-ALT-{self._alert_counter:08d}"

    def _generate_hour_id(self) -> str:
        self._hour_counter += 1
        return f"ARB-HR-{self._hour_counter:08d}"

    def record_dispatch(self, decision: DispatchDecision, now: datetime,
                        time_interval_hours: float,
                        storage_mode: str = "normal",
                        plan_active: bool = True,
                        plan_abnormal: bool = False,
                        soc_before: float = 0.0,
                        soc_after: float = 0.0,
                        charge_from_grid_kw: float = 0.0,
                        charge_from_renewable_kw: float = 0.0) -> None:
        if not self.enabled:
            return

        tariff_period = config.get_tariff_period(now.hour)
        bes_id = list(config.BESS_CONFIG.keys())[0]
        bess_action = decision.bess_action.get(bes_id, {})

        actual_charge_kw = bess_action.get("charge_kw", 0.0)
        actual_discharge_kw = bess_action.get("discharge_kw", 0.0)

        is_active_arbitrage = False
        if tariff_period == "valley" and storage_mode == "active_charge" and plan_active and not plan_abnormal:
            is_active_arbitrage = True
        elif tariff_period == "peak" and storage_mode == "priority_discharge" and plan_active and not plan_abnormal:
            is_active_arbitrage = True

        interrupted = False
        interruption_reason = ""
        if plan_active and not plan_abnormal and is_active_arbitrage:
            if tariff_period == "valley" and storage_mode == "active_charge":
                if actual_charge_kw <= 0.01:
                    interrupted = True
                    interruption_reason = self._detect_interruption_reason(decision, now, "charge")
            elif tariff_period == "peak" and storage_mode == "priority_discharge":
                if actual_discharge_kw <= 0.01:
                    interrupted = True
                    interruption_reason = self._detect_interruption_reason(decision, now, "discharge")

        record = ArbitrageHourlyRecord(
            timestamp=now,
            hour=now.hour,
            tariff_period=tariff_period,
            planned_mode=storage_mode,
            actual_charge_kw=actual_charge_kw,
            actual_discharge_kw=actual_discharge_kw,
            charge_from_grid_kw=charge_from_grid_kw,
            charge_from_renewable_kw=charge_from_renewable_kw,
            is_active_arbitrage=is_active_arbitrage,
            interrupted=interrupted,
            interruption_reason=interruption_reason,
            time_interval_hours=time_interval_hours,
            grid_buy_price=decision.grid_buy_price,
            soc_before=soc_before,
            soc_after=soc_after,
        )

        self.hourly_records.append(record)

        if interrupted:
            self._record_interruption(record, decision, now)

    def _detect_interruption_reason(self, decision: DispatchDecision, now: datetime,
                                     action_type: str) -> str:
        for note in decision.notes:
            if "需求响应" in note or "DR" in note or "demand_response" in note.lower():
                return "demand_response"
            if "碳" in note or "carbon" in note.lower():
                return "carbon_constraint"
            if "SOC" in note or "soc" in note:
                return "soc_limit"
            if "甩负荷" in note or "shed" in note.lower():
                return "load_shed_priority"
            if "故障" in note or "fault" in note.lower() or "异常" in note:
                return "equipment_fault"
        return "unknown"

    def _record_interruption(self, hour_record: ArbitrageHourlyRecord,
                              decision: DispatchDecision, now: datetime) -> None:
        bes_id = list(config.BESS_CONFIG.keys())[0]
        cfg = config.BESS_CONFIG[bes_id]
        valley_price = config.get_valley_price()
        peak_price = config.get_peak_price()
        charge_eff = cfg["charge_efficiency"]
        discharge_eff = cfg["discharge_efficiency"]

        lost_charge_kwh = 0.0
        lost_discharge_kwh = 0.0
        lost_revenue = 0.0

        if hour_record.planned_mode == "active_charge":
            lost_charge_kwh = cfg["max_charge_power"] * hour_record.time_interval_hours
            lost_revenue = lost_charge_kwh * (peak_price * discharge_eff - valley_price / charge_eff)
        elif hour_record.planned_mode == "priority_discharge":
            lost_discharge_kwh = cfg["max_discharge_power"] * hour_record.time_interval_hours
            lost_revenue = lost_discharge_kwh * (peak_price - valley_price / charge_eff / discharge_eff)

        reason_cn = {
            "demand_response": "需求响应",
            "carbon_constraint": "碳约束",
            "soc_limit": "电池SOC限制",
            "load_shed_priority": "负荷优先供电",
            "equipment_fault": "设备故障",
            "unknown": "未知原因",
        }.get(hour_record.interruption_reason, "未知原因")

        interruption = ArbitrageInterruptionRecord(
            interruption_id=self._generate_interruption_id(),
            timestamp=now,
            hour=now.hour,
            reason=reason_cn,
            reason_category=hour_record.interruption_reason,
            planned_mode=hour_record.planned_mode,
            lost_charge_kwh=round(lost_charge_kwh, 4),
            lost_discharge_kwh=round(lost_discharge_kwh, 4),
            lost_revenue=round(max(0.0, lost_revenue), 4),
            details={
                "notes": decision.notes,
                "grid_import_kw": decision.grid_import_kw,
                "grid_export_kw": decision.grid_export_kw,
                "load_shed_kw": decision.load_shed_kw,
            },
        )

        date_str = now.strftime("%Y-%m-%d")
        if date_str not in self.settlements:
            pass

    def _get_date_hourly_records(self, date_str: str) -> List[ArbitrageHourlyRecord]:
        records = []
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        for rec in self.hourly_records:
            if rec.timestamp.date() == target_date:
                records.append(rec)
        return records

    def _calculate_valley_hours(self) -> int:
        count = 0
        for period, info in config.GRID_TARIFF.items():
            if period == "valley":
                for start, end in info["hours"]:
                    if start < end:
                        count += end - start
                    else:
                        count += (24 - start) + end
        return count

    def _calculate_peak_hours(self) -> int:
        count = 0
        for period, info in config.GRID_TARIFF.items():
            if period == "peak":
                for start, end in info["hours"]:
                    if start < end:
                        count += end - start
                    else:
                        count += (24 - start) + end
        return count

    def settle_day(self, settlement_date: str, is_recalculation: bool = False) -> ArbitrageSettlementDetail:
        if not self.enabled:
            raise ValueError("套利分析功能未启用")

        bes_id = list(config.BESS_CONFIG.keys())[0]
        cfg = config.BESS_CONFIG[bes_id]
        charge_eff = cfg["charge_efficiency"]
        discharge_eff = cfg["discharge_efficiency"]
        valley_price = config.get_valley_price()
        peak_price = config.get_peak_price()

        records = self._get_date_hourly_records(settlement_date)

        valley_active_charge_kwh = 0.0
        valley_passive_charge_kwh = 0.0
        peak_discharge_kwh = 0.0
        interruptions: List[ArbitrageInterruptionRecord] = []
        total_lost_revenue = 0.0

        for rec in records:
            charge_kwh = rec.actual_charge_kw * rec.time_interval_hours
            discharge_kwh = rec.actual_discharge_kw * rec.time_interval_hours

            if rec.tariff_period == "valley":
                if rec.is_active_arbitrage and rec.charge_from_grid_kw > 0:
                    valley_active_charge_kwh += charge_kwh
                elif charge_kwh > 0:
                    if config.ARBITRAGE_ANALYSIS_CONFIG.get("passive_charge_from_renewable", True):
                        valley_passive_charge_kwh += charge_kwh
            elif rec.tariff_period == "peak":
                peak_discharge_kwh += discharge_kwh

            if rec.interrupted:
                lost = self._calculate_hour_lost_revenue(rec, cfg, valley_price, peak_price,
                                                          charge_eff, discharge_eff)
                reason_cn = {
                    "demand_response": "需求响应",
                    "carbon_constraint": "碳约束",
                    "soc_limit": "电池SOC限制",
                    "load_shed_priority": "负荷优先供电",
                    "equipment_fault": "设备故障",
                    "unknown": "未知原因",
                }.get(rec.interruption_reason, "未知原因")

                interruption = ArbitrageInterruptionRecord(
                    interruption_id=self._generate_interruption_id(),
                    timestamp=rec.timestamp,
                    hour=rec.hour,
                    reason=reason_cn,
                    reason_category=rec.interruption_reason,
                    planned_mode=rec.planned_mode,
                    lost_charge_kwh=round(cfg["max_charge_power"] * rec.time_interval_hours, 4)
                    if rec.planned_mode == "active_charge" else 0.0,
                    lost_discharge_kwh=round(cfg["max_discharge_power"] * rec.time_interval_hours, 4)
                    if rec.planned_mode == "priority_discharge" else 0.0,
                    lost_revenue=round(lost, 4),
                    details={"grid_buy_price": rec.grid_buy_price},
                )
                interruptions.append(interruption)
                total_lost_revenue += lost

        total_charge_kwh = valley_active_charge_kwh + valley_passive_charge_kwh
        theoretical_revenue = 0.0
        if total_charge_kwh > 0 and peak_discharge_kwh > 0:
            effective_discharge = min(peak_discharge_kwh, total_charge_kwh * charge_eff * discharge_eff)
            theoretical_revenue = effective_discharge * peak_price - (valley_active_charge_kwh * valley_price)

        charge_loss_cost = valley_active_charge_kwh * valley_price * (1 - charge_eff) / charge_eff
        discharge_loss_cost = peak_discharge_kwh * peak_price * (1 - discharge_eff)
        efficiency_loss_cost = charge_loss_cost + discharge_loss_cost

        net_revenue = theoretical_revenue - efficiency_loss_cost

        valley_hours = self._calculate_valley_hours()
        peak_hours = self._calculate_peak_hours()

        max_charge_per_valley_hour = cfg["max_charge_power"]
        max_discharge_per_peak_hour = cfg["max_discharge_power"]

        theoretical_max_charge = valley_hours * max_charge_per_valley_hour
        effective_max_discharge = theoretical_max_charge * charge_eff * discharge_eff
        theoretical_max_revenue = effective_max_discharge * peak_price - theoretical_max_charge * valley_price
        theoretical_max_efficiency_loss = (
            theoretical_max_charge * valley_price * (1 - charge_eff) / charge_eff
            + effective_max_discharge * peak_price * (1 - discharge_eff)
        )
        theoretical_max_net_revenue = theoretical_max_revenue - theoretical_max_efficiency_loss

        execution_rate = 0.0
        if theoretical_max_net_revenue > 0:
            execution_rate = max(0.0, min(100.0, net_revenue / theoretical_max_net_revenue * 100))

        baseline_savings = 0.0
        if peak_discharge_kwh > 0:
            baseline_savings = peak_discharge_kwh * (peak_price - valley_price)

        notes = []
        if valley_active_charge_kwh == 0 and valley_passive_charge_kwh == 0:
            notes.append("当日谷时段无充电记录")
        if peak_discharge_kwh == 0:
            notes.append("当日峰时段无放电记录")
        if execution_rate < 50:
            notes.append(f"执行率较低({execution_rate:.1f}%)，建议检查储能计划配置")

        settlement = ArbitrageSettlementDetail(
            settlement_id=self._generate_settlement_id(),
            settlement_date=settlement_date,
            generated_at=datetime.now(),
            is_recalculation=is_recalculation,
            valley_active_charge_kwh=round(valley_active_charge_kwh, 4),
            valley_passive_charge_kwh=round(valley_passive_charge_kwh, 4),
            peak_discharge_kwh=round(peak_discharge_kwh, 4),
            valley_charge_price=valley_price,
            peak_discharge_price=peak_price,
            theoretical_revenue=round(theoretical_revenue, 4),
            efficiency_loss_cost=round(efficiency_loss_cost, 4),
            net_revenue=round(net_revenue, 4),
            theoretical_max_net_revenue=round(theoretical_max_net_revenue, 4),
            execution_rate=round(execution_rate, 2),
            interruptions=interruptions,
            interruption_count=len(interruptions),
            total_lost_revenue=round(total_lost_revenue, 4),
            valley_hours_count=valley_hours,
            peak_hours_count=peak_hours,
            baseline_savings=round(baseline_savings, 4),
            notes=notes,
            charge_efficiency=charge_eff,
            discharge_efficiency=discharge_eff,
            battery_capacity_kwh=cfg["capacity_kwh"],
            max_charge_power_kw=cfg["max_charge_power"],
            max_discharge_power_kw=cfg["max_discharge_power"],
        )

        self.settlements[settlement_date] = settlement
        self._last_settlement_date = settlement_date

        self._check_consecutive_low_execution_rate(settlement_date)

        return settlement

    def _calculate_hour_lost_revenue(self, rec: ArbitrageHourlyRecord, cfg: Dict,
                                       valley_price: float, peak_price: float,
                                       charge_eff: float, discharge_eff: float) -> float:
        if rec.planned_mode == "active_charge":
            max_charge_kwh = cfg["max_charge_power"] * rec.time_interval_hours
            return max(0.0, max_charge_kwh * (peak_price * discharge_eff - valley_price / charge_eff))
        elif rec.planned_mode == "priority_discharge":
            max_discharge_kwh = cfg["max_discharge_power"] * rec.time_interval_hours
            return max(0.0, max_discharge_kwh * (peak_price - valley_price / charge_eff / discharge_eff))
        return 0.0

    def _check_consecutive_low_execution_rate(self, current_date_str: str) -> None:
        threshold = config.ARBITRAGE_ANALYSIS_CONFIG.get("low_execution_rate_threshold", 50.0)
        consecutive_days = config.ARBITRAGE_ANALYSIS_CONFIG.get("consecutive_low_days_threshold", 3)

        current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()

        low_dates: List[str] = []
        for i in range(consecutive_days):
            check_date = current_date - timedelta(days=i)
            check_date_str = check_date.strftime("%Y-%m-%d")
            settlement = self.settlements.get(check_date_str)
            if settlement and settlement.execution_rate < threshold:
                low_dates.append(check_date_str)
            else:
                break

        if len(low_dates) >= consecutive_days:
            self._generate_low_execution_alert(low_dates, threshold)

    def _generate_low_execution_alert(self, low_dates: List[str], threshold: float) -> None:
        existing_alerts = [
            a for a in self.alerts
            if a.alert_type == "LOW_EXECUTION_RATE" and not a.acknowledged
        ]
        if existing_alerts:
            return

        avg_rate = 0.0
        total_net = 0.0
        for d in low_dates:
            s = self.settlements.get(d)
            if s:
                avg_rate += s.execution_rate
                total_net += s.net_revenue
        avg_rate = avg_rate / len(low_dates) if low_dates else 0.0

        message = (
            f"连续{len(low_dates)}天套利执行率低于{threshold:.0f}%，"
            f"平均执行率{avg_rate:.1f}%，累计净收益{total_net:.2f}元，"
            f"建议检查储能计划配置"
        )

        alert = ArbitrageAlert(
            alert_id=self._generate_alert_id(),
            timestamp=datetime.now(),
            alert_type="LOW_EXECUTION_RATE",
            alert_level="WARNING",
            message=message,
            details={
                "low_dates": low_dates,
                "threshold": threshold,
                "avg_execution_rate": round(avg_rate, 2),
                "total_net_revenue": round(total_net, 4),
                "suggestions": [
                    "检查峰谷电价配置是否正确",
                    "检查电池SOC工作区间是否合理",
                    "检查需求响应和碳约束配置是否过于严格",
                    "查看中断记录了解具体原因",
                ],
            },
        )

        self.alerts.append(alert)

        self.state.add_alert(
            "ARBITRAGE_LOW_EXECUTION_RATE",
            message,
            alert.details,
        )

    def get_settlement(self, date_str: str) -> Optional[ArbitrageSettlementDetail]:
        return self.settlements.get(date_str)

    def get_settlement_summary(self, start_date_str: str, end_date_str: str) -> ArbitrageSettlementSummary:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")

        settlements = []
        current = start_date
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            settlement = self.settlements.get(date_str)
            if settlement:
                settlements.append(settlement)
            current += timedelta(days=1)

        threshold = config.ARBITRAGE_ANALYSIS_CONFIG.get("low_execution_rate_threshold", 50.0)

        total_net = sum(s.net_revenue for s in settlements)
        total_theoretical = sum(s.theoretical_revenue for s in settlements)
        total_eff_loss = sum(s.efficiency_loss_cost for s in settlements)
        total_interruptions = sum(s.interruption_count for s in settlements)
        total_lost = sum(s.total_lost_revenue for s in settlements)
        total_active_charge = sum(s.valley_active_charge_kwh for s in settlements)
        total_passive_charge = sum(s.valley_passive_charge_kwh for s in settlements)
        total_discharge = sum(s.peak_discharge_kwh for s in settlements)
        total_baseline = sum(s.baseline_savings for s in settlements)

        avg_rate = 0.0
        if settlements:
            avg_rate = sum(s.execution_rate for s in settlements) / len(settlements)

        low_days = sum(1 for s in settlements if s.execution_rate < threshold)
        high_days = sum(1 for s in settlements if s.execution_rate >= 80)

        return ArbitrageSettlementSummary(
            start_date=start_date_str,
            end_date=end_date_str,
            settlement_count=len(settlements),
            total_net_revenue=round(total_net, 4),
            total_theoretical_revenue=round(total_theoretical, 4),
            total_efficiency_loss_cost=round(total_eff_loss, 4),
            avg_execution_rate=round(avg_rate, 2),
            total_interruptions=total_interruptions,
            total_lost_revenue=round(total_lost, 4),
            total_valley_active_charge_kwh=round(total_active_charge, 4),
            total_valley_passive_charge_kwh=round(total_passive_charge, 4),
            total_peak_discharge_kwh=round(total_discharge, 4),
            total_baseline_savings=round(total_baseline, 4),
            low_execution_rate_days=low_days,
            high_execution_rate_days=high_days,
        )

    def get_trend(self, start_date_str: str, end_date_str: str) -> List[ArbitrageTrendPoint]:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()

        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")

        trend = []
        current = start_date
        while current <= end_date:
            date_str = current.strftime("%Y-%m-%d")
            settlement = self.settlements.get(date_str)
            if settlement:
                point = ArbitrageTrendPoint(
                    date=date_str,
                    net_revenue=round(settlement.net_revenue, 4),
                    execution_rate=round(settlement.execution_rate, 2),
                    theoretical_revenue=round(settlement.theoretical_revenue, 4),
                    interruption_count=settlement.interruption_count,
                    valley_charge_kwh=round(
                        settlement.valley_active_charge_kwh + settlement.valley_passive_charge_kwh, 4
                    ),
                    peak_discharge_kwh=round(settlement.peak_discharge_kwh, 4),
                )
            else:
                point = ArbitrageTrendPoint(
                    date=date_str,
                    net_revenue=0.0,
                    execution_rate=0.0,
                    theoretical_revenue=0.0,
                    interruption_count=0,
                    valley_charge_kwh=0.0,
                    peak_discharge_kwh=0.0,
                )
            trend.append(point)
            current += timedelta(days=1)

        return trend

    def resettle_day(self, settlement_date: str) -> ArbitrageSettlementDetail:
        if settlement_date in self.settlements:
            pass
        return self.settle_day(settlement_date, is_recalculation=True)

    def get_alerts(self, acknowledged: Optional[bool] = None,
                   limit: int = 50) -> List[ArbitrageAlert]:
        alerts = self.alerts
        if acknowledged is not None:
            alerts = [a for a in alerts if a.acknowledged == acknowledged]
        return alerts[-limit:]

    def acknowledge_alert(self, alert_id: str, acknowledged_by: Optional[str] = None) -> bool:
        for alert in self.alerts:
            if alert.alert_id == alert_id and not alert.acknowledged:
                alert.acknowledged = True
                alert.acknowledged_at = datetime.now()
                alert.acknowledged_by = acknowledged_by
                return True
        return False

    def should_auto_settle(self, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()

        if not self.enabled:
            return False

        cfg = config.ARBITRAGE_ANALYSIS_CONFIG
        settle_hour = cfg.get("settlement_hour", 0)
        settle_minute = cfg.get("settlement_minute", 0)

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        if now.hour == settle_hour and now.minute >= settle_minute:
            if self._last_settlement_date != yesterday:
                return True

        return False

    def auto_settle_if_due(self, now: datetime = None) -> Optional[ArbitrageSettlementDetail]:
        if now is None:
            now = datetime.now()

        if not self.should_auto_settle(now):
            return None

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            settlement = self.settle_day(yesterday, is_recalculation=False)
            return settlement
        except Exception:
            return None

    def get_hourly_records(self, date_str: str) -> List[ArbitrageHourlyRecord]:
        return self._get_date_hourly_records(date_str)
