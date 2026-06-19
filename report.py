from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import asdict

import config
from models import (
    MicrogridState, DailyReport, WeeklyReport,
    TopExpensiveDispatch, BatteryDailyStats,
    LoadGroupReliability, StrategySuggestion,
    CostAttribution
)


class ReportGenerator:
    def __init__(self, state: MicrogridState):
        self.state = state
        self._report_counter = 0

    def _generate_report_id(self, report_type: str) -> str:
        self._report_counter += 1
        prefix = "DAILY" if report_type == "daily" else "WEEKLY"
        return f"{prefix}-{self._report_counter:08d}"

    def _get_date_range(self, date_str: str) -> Tuple[datetime, datetime]:
        date = datetime.strptime(date_str, "%Y-%m-%d")
        start = datetime(date.year, date.month, date.day, 0, 0, 0)
        end = datetime(date.year, date.month, date.day, 23, 59, 59)
        return start, end

    def _filter_cost_attributions_by_date(self, date_str: str) -> List[CostAttribution]:
        start, end = self._get_date_range(date_str)
        result = []
        for attr in self.state.cost_attributions:
            if start <= attr.timestamp <= end:
                result.append(attr)
        return result

    def _compute_daily_cost_summary(self, date_str: str) -> Dict[str, Any]:
        attrs = self._filter_cost_attributions_by_date(date_str)
        
        total_grid = sum(a.grid_purchase_cost for a in attrs)
        total_diesel_gen = sum(a.diesel_generation_cost for a in attrs)
        total_diesel_startup = sum(a.diesel_startup_cost for a in attrs)
        total_diesel = total_diesel_gen + total_diesel_startup
        total_shed = sum(a.load_shed_penalty_cost for a in attrs)
        total_feedin = sum(a.feed_in_revenue for a in attrs)
        total_net = total_grid + total_diesel + total_shed - total_feedin

        total_grid_import_kwh = sum(a.details.get("grid_import_kwh", 0) for a in attrs)
        total_feedin_kwh = sum(a.details.get("feed_in_kwh", 0) for a in attrs)

        valley_import_kwh = 0.0
        for a in attrs:
            if a.details.get("tariff_period") == "valley":
                valley_import_kwh += a.details.get("grid_import_kwh", 0)

        valley_ratio = valley_import_kwh / total_grid_import_kwh if total_grid_import_kwh > 0 else 0.0

        return {
            "dispatch_count": len(attrs),
            "grid_purchase_cost": round(total_grid, 4),
            "diesel_generation_cost": round(total_diesel_gen, 4),
            "diesel_startup_cost": round(total_diesel_startup, 4),
            "diesel_total_cost": round(total_diesel, 4),
            "load_shed_penalty": round(total_shed, 4),
            "feed_in_revenue": round(total_feedin, 4),
            "net_cost": round(total_net, 4),
            "total_grid_import_kwh": round(total_grid_import_kwh, 4),
            "total_feed_in_kwh": round(total_feedin_kwh, 4),
            "valley_grid_import_kwh": round(valley_import_kwh, 4),
            "valley_purchase_ratio": round(valley_ratio, 4),
        }

    def _get_top_expensive_dispatches(self, date_str: str, n: int = 3) -> List[TopExpensiveDispatch]:
        attrs = self._filter_cost_attributions_by_date(date_str)
        sorted_attrs = sorted(attrs, key=lambda x: x.total_comprehensive_cost, reverse=True)
        top_attrs = sorted_attrs[:n]

        result = []
        for attr in top_attrs:
            breakdown = {
                "grid_purchase": attr.grid_purchase_cost,
                "diesel_generation": attr.diesel_generation_cost,
                "diesel_startup": attr.diesel_startup_cost,
                "load_shed_penalty": attr.load_shed_penalty_cost,
                "bess_loss": attr.bess_loss_cost,
                "feed_in_revenue": -attr.feed_in_revenue,
            }
            
            reason = self._generate_dispatch_reason(attr)
            
            result.append(TopExpensiveDispatch(
                dispatch_id=attr.dispatch_id,
                timestamp=attr.timestamp,
                total_cost=round(attr.total_comprehensive_cost, 4),
                cost_breakdown=breakdown,
                reason=reason,
            ))
        return result

    def _generate_dispatch_reason(self, attr: CostAttribution) -> str:
        reasons = []
        
        if attr.diesel_generation_cost > 0:
            reasons.append("柴油机发电")
        if attr.diesel_startup_cost > 0:
            reasons.append("柴油机启动")
        if attr.load_shed_penalty_cost > 0:
            reasons.append("甩负荷惩罚")
        if attr.grid_purchase_cost > 10:
            reasons.append("高价购电")
        
        if not reasons:
            reasons.append("常规调度")
        
        return "、".join(reasons)

    def _compute_battery_daily_stats(self, date_str: str) -> Dict[str, BatteryDailyStats]:
        start, end = self._get_date_range(date_str)
        result = {}

        for bes_id in config.BESS_CONFIG:
            total_charged = 0.0
            total_discharged = 0.0
            soc_min = 1.0
            soc_max = 0.0
            start_soc = None
            end_soc = None
            start_cycles = None
            end_cycles = None

            for decision in self.state.dispatch_history:
                if start <= decision.timestamp <= end:
                    bess_action = decision.bess_action.get(bes_id, {})
                    
                    charge_kw = bess_action.get("charge_kw", 0)
                    discharge_kw = bess_action.get("discharge_kw", 0)
                    soc_before = bess_action.get("soc_before", 0)
                    soc_after = bess_action.get("soc_after", 0)

                    if start_soc is None:
                        start_soc = soc_before
                    
                    end_soc = soc_after
                    
                    time_interval = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0
                    total_charged += charge_kw * time_interval
                    total_discharged += discharge_kw * time_interval
                    
                    soc_min = min(soc_min, soc_before, soc_after)
                    soc_max = max(soc_max, soc_before, soc_after)

            bs = self.state.bess_state.get(bes_id)
            if bs:
                end_cycles = bs.health.equivalent_cycles

            cycle_increment = 0.0
            if start_soc is not None and end_soc is not None:
                capacity_kwh = config.BESS_CONFIG[bes_id]["capacity_kwh"]
                cycle_increment = total_discharged / capacity_kwh if capacity_kwh > 0 else 0.0

            if start_soc is None:
                start_soc = 0.5
                end_soc = 0.5
                soc_min = 0.5
                soc_max = 0.5

            result[bes_id] = BatteryDailyStats(
                total_charged_kwh=round(total_charged, 4),
                total_discharged_kwh=round(total_discharged, 4),
                soc_min=round(soc_min, 4),
                soc_max=round(soc_max, 4),
                cycle_increment=round(cycle_increment, 4),
                start_soc=round(start_soc, 4),
                end_soc=round(end_soc, 4),
            )

        return result

    def _compute_load_group_reliability(self, date_str: str) -> List[LoadGroupReliability]:
        start, end = self._get_date_range(date_str)
        result = []

        for gid in config.LOAD_GROUP_CONFIG:
            stats = self.state.get_load_group_reliability_stats(gid, start, end)
            gs = self.state.load_group_state.get(gid, {})
            group_name = gs.get("name", gid)
            
            result.append(LoadGroupReliability(
                group_id=gid,
                group_name=group_name,
                reliability_percent=stats.get("reliability_percent", 100.0) or 100.0,
                total_snapshots=stats.get("total_snapshots", 0),
                shed_snapshots=stats.get("shed_snapshots", 0),
            ))

        return result

    def _compute_load_shed_stats(self, date_str: str) -> Tuple[int, float]:
        start, end = self._get_date_range(date_str)
        events = []
        
        for event in self.state.load_group_shed_events:
            if start <= event.started_at <= end:
                events.append(event)

        total_duration = sum(
            e.duration_minutes for e in events if e.ended_at is not None
        )
        
        return len(events), round(total_duration, 2)

    def _compute_renewable_surplus(self, date_str: str) -> float:
        start, end = self._get_date_range(date_str)
        surplus_kwh = 0.0

        for decision in self.state.dispatch_history:
            if start <= decision.timestamp <= end:
                if decision.grid_export_kw > 0:
                    time_interval = config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0
                    surplus_kwh += decision.grid_export_kw * time_interval

        return round(surplus_kwh, 4)

    def _generate_strategy_suggestions(self, 
                                       date_str: str, 
                                       cost_summary: Dict[str, Any],
                                       battery_stats: Dict[str, BatteryDailyStats],
                                       load_shed_count: int) -> List[StrategySuggestion]:
        suggestions = []

        if load_shed_count > 3:
            suggestions.append(StrategySuggestion(
                type="CAPACITY_EXPANSION",
                severity="high",
                title="建议扩容",
                description=f"当日甩负荷{load_shed_count}次，超过3次预警阈值，供电容量不足，建议扩容发电机组或储能系统。",
                data={"shed_count": load_shed_count, "threshold": 3}
            ))

        valley_ratio = cost_summary.get("valley_purchase_ratio", 0)
        if valley_ratio < 0.3 and cost_summary.get("total_grid_import_kwh", 0) > 0:
            suggestions.append(StrategySuggestion(
                type="VALLEY_CHARGE_OPTIMIZATION",
                severity="medium",
                title="谷时充电力度不足",
                description=f"谷时段购电占比仅{valley_ratio*100:.1f}%，低于30%的建议阈值。建议增加谷时段储能充电量，以降低购电成本。",
                data={"valley_ratio": valley_ratio, "threshold": 0.3}
            ))

        for bes_id, bstats in battery_stats.items():
            cycle_life = config.BESS_CONFIG[bes_id]["cycle_life_threshold"]
            cycle_increment = bstats.cycle_increment
            cycle_percent = cycle_increment / cycle_life if cycle_life > 0 else 0
            
            if cycle_percent > 0.01:
                suggestions.append(StrategySuggestion(
                    type="BATTERY_OVERUSE",
                    severity="high",
                    title="电池使用强度过高",
                    description=f"电池{bes_id}当日等效循环增量{cycle_increment:.2f}次，占寿命的{cycle_percent*100:.2f}%，超过1%警告阈值。建议优化充放电策略，延长电池寿命。",
                    data={
                        "bes_id": bes_id,
                        "cycle_increment": cycle_increment,
                        "cycle_life": cycle_life,
                        "cycle_percent": cycle_percent,
                        "threshold": 0.01
                    }
                ))

        renewable_surplus = self._compute_renewable_surplus(date_str)
        feedin_revenue = cost_summary.get("feed_in_revenue", 0)
        if renewable_surplus > 0 and feedin_revenue <= 0.001:
            suggestions.append(StrategySuggestion(
                type="GRID_EXPORT_CONFIG",
                severity="medium",
                title="上网售电配置可能异常",
                description=f"检测到新能源盈余{renewable_surplus:.2f}kWh，但售电收入为0。请检查电网并网配置和售电价设置。",
                data={"renewable_surplus_kwh": renewable_surplus, "feed_in_revenue": feedin_revenue}
            ))

        return suggestions

    def generate_daily_report(self, date_str: str = None) -> DailyReport:
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        report_id = self._generate_report_id("daily")
        
        cost_summary = self._compute_daily_cost_summary(date_str)
        top_expensive = self._get_top_expensive_dispatches(date_str, 3)
        battery_stats = self._compute_battery_daily_stats(date_str)
        load_group_reliability = self._compute_load_group_reliability(date_str)
        shed_count, shed_duration = self._compute_load_shed_stats(date_str)

        prev_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_cost_summary = self._compute_daily_cost_summary(prev_date)
        prev_net_cost = prev_cost_summary["net_cost"] if prev_cost_summary["dispatch_count"] > 0 else None
        
        net_cost_change_percent = None
        if prev_net_cost is not None and prev_net_cost > 0:
            net_cost_change_percent = round(
                (cost_summary["net_cost"] - prev_net_cost) / prev_net_cost * 100, 2
            )

        suggestions = self._generate_strategy_suggestions(
            date_str, cost_summary, battery_stats, shed_count
        )

        renewable_surplus = self._compute_renewable_surplus(date_str)

        report = DailyReport(
            report_id=report_id,
            report_type="daily",
            report_date=date_str,
            generated_at=datetime.now(),
            dispatch_count=cost_summary["dispatch_count"],
            grid_purchase_cost=cost_summary["grid_purchase_cost"],
            diesel_total_cost=cost_summary["diesel_total_cost"],
            diesel_generation_cost=cost_summary["diesel_generation_cost"],
            diesel_startup_cost=cost_summary["diesel_startup_cost"],
            load_shed_penalty=cost_summary["load_shed_penalty"],
            feed_in_revenue=cost_summary["feed_in_revenue"],
            net_cost=cost_summary["net_cost"],
            prev_day_net_cost=prev_net_cost,
            net_cost_change_percent=net_cost_change_percent,
            top_expensive_dispatches=top_expensive,
            battery_stats=battery_stats,
            load_group_reliability=load_group_reliability,
            valley_purchase_ratio=cost_summary["valley_purchase_ratio"],
            total_grid_import_kwh=cost_summary["total_grid_import_kwh"],
            valley_grid_import_kwh=cost_summary["valley_grid_import_kwh"],
            suggestions=suggestions,
            total_load_shed_events=shed_count,
            total_load_shed_duration_minutes=shed_duration,
            renewable_surplus_kwh=renewable_surplus,
        )

        return report

    def generate_weekly_report(self, start_date_str: str, end_date_str: str) -> WeeklyReport:
        report_id = self._generate_report_id("weekly")
        
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        
        daily_reports = []
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            daily_report = self.generate_daily_report(date_str)
            daily_reports.append(daily_report)
            current_date += timedelta(days=1)

        total_dispatch = sum(r.dispatch_count for r in daily_reports)
        total_grid_cost = sum(r.grid_purchase_cost for r in daily_reports)
        total_diesel_cost = sum(r.diesel_total_cost for r in daily_reports)
        total_shed_penalty = sum(r.load_shed_penalty for r in daily_reports)
        total_feedin = sum(r.feed_in_revenue for r in daily_reports)
        total_net = sum(r.net_cost for r in daily_reports)

        num_days = len(daily_reports)
        avg_dispatch = total_dispatch / num_days if num_days > 0 else 0
        avg_grid_cost = total_grid_cost / num_days if num_days > 0 else 0
        avg_net_cost = total_net / num_days if num_days > 0 else 0

        daily_trend = []
        for r in daily_reports:
            daily_trend.append({
                "date": r.report_date,
                "dispatch_count": r.dispatch_count,
                "grid_purchase_cost": r.grid_purchase_cost,
                "diesel_total_cost": r.diesel_total_cost,
                "load_shed_penalty": r.load_shed_penalty,
                "feed_in_revenue": r.feed_in_revenue,
                "net_cost": r.net_cost,
                "valley_purchase_ratio": r.valley_purchase_ratio,
            })

        sorted_by_cost = sorted(daily_reports, key=lambda r: r.net_cost, reverse=True)
        most_expensive_day = sorted_by_cost[0].report_date if sorted_by_cost else ""
        most_expensive_cost = sorted_by_cost[0].net_cost if sorted_by_cost else 0
        cheapest_day = sorted_by_cost[-1].report_date if sorted_by_cost else ""
        cheapest_cost = sorted_by_cost[-1].net_cost if sorted_by_cost else 0

        arbitrage_stats = self.state.get_arbitrage_stats_report()
        storage_arbitrage_profit = arbitrage_stats.get("net_profit", 0)

        total_shed_events = sum(r.total_load_shed_events for r in daily_reports)
        total_shed_duration = sum(r.total_load_shed_duration_minutes for r in daily_reports)

        suggestions = self._generate_weekly_suggestions(daily_reports)

        return WeeklyReport(
            report_id=report_id,
            report_type="weekly",
            start_date=start_date_str,
            end_date=end_date_str,
            generated_at=datetime.now(),
            total_dispatch_count=total_dispatch,
            avg_daily_dispatch_count=round(avg_dispatch, 2),
            total_grid_purchase_cost=round(total_grid_cost, 4),
            avg_daily_grid_purchase_cost=round(avg_grid_cost, 4),
            total_diesel_cost=round(total_diesel_cost, 4),
            total_load_shed_penalty=round(total_shed_penalty, 4),
            total_feed_in_revenue=round(total_feedin, 4),
            total_net_cost=round(total_net, 4),
            avg_daily_net_cost=round(avg_net_cost, 4),
            most_expensive_day=most_expensive_day,
            most_expensive_day_cost=round(most_expensive_cost, 4),
            cheapest_day=cheapest_day,
            cheapest_day_cost=round(cheapest_cost, 4),
            daily_trend=daily_trend,
            storage_arbitrage_profit=round(storage_arbitrage_profit, 4),
            total_load_shed_events=total_shed_events,
            total_load_shed_duration_minutes=round(total_shed_duration, 2),
            suggestions=suggestions,
        )

    def _generate_weekly_suggestions(self, daily_reports: List[DailyReport]) -> List[StrategySuggestion]:
        suggestions = []

        total_shed_events = sum(r.total_load_shed_events for r in daily_reports)
        if total_shed_events > 21:
            suggestions.append(StrategySuggestion(
                type="WEEKLY_CAPACITY_WARNING",
                severity="high",
                title="周容量预警",
                description=f"本周累计甩负荷{total_shed_events}次，日均超过3次，供电容量严重不足，强烈建议扩容。",
                data={"total_shed_events": total_shed_events, "days": len(daily_reports)}
            ))

        avg_valley_ratio = 0.0
        valid_days = 0
        for r in daily_reports:
            if r.total_grid_import_kwh > 0:
                avg_valley_ratio += r.valley_purchase_ratio
                valid_days += 1
        
        if valid_days > 0:
            avg_valley_ratio /= valid_days
            if avg_valley_ratio < 0.3:
                suggestions.append(StrategySuggestion(
                    type="WEEKLY_VALLEY_CHARGE_WARNING",
                    severity="medium",
                    title="周谷时充电不足",
                    description=f"本周平均谷时段购电占比{avg_valley_ratio*100:.1f}%，低于30%建议值。建议优化储能充放电策略，增加谷时充电。",
                    data={"avg_valley_ratio": avg_valley_ratio, "threshold": 0.3}
                ))

        for bes_id in config.BESS_CONFIG:
            total_cycle_increment = sum(
                r.battery_stats.get(bes_id, BatteryDailyStats(0,0,1,0,0,0,0)).cycle_increment
                for r in daily_reports
            )
            cycle_life = config.BESS_CONFIG[bes_id]["cycle_life_threshold"]
            cycle_percent = total_cycle_increment / cycle_life if cycle_life > 0 else 0
            
            if cycle_percent > 0.07:
                suggestions.append(StrategySuggestion(
                    type="WEEKLY_BATTERY_OVERUSE",
                    severity="high",
                    title="周电池使用过度",
                    description=f"电池{bes_id}本周累计循环增量{total_cycle_increment:.2f}次，占寿命{cycle_percent*100:.2f}%，超过7%周警告值。",
                    data={
                        "bes_id": bes_id,
                        "weekly_cycle_increment": total_cycle_increment,
                        "cycle_life": cycle_life,
                        "cycle_percent": cycle_percent,
                        "weekly_threshold": 0.07
                    }
                ))

        total_surplus = sum(r.renewable_surplus_kwh for r in daily_reports)
        total_feedin = sum(r.feed_in_revenue for r in daily_reports)
        if total_surplus > 10 and total_feedin <= 0.01:
            suggestions.append(StrategySuggestion(
                type="WEEKLY_GRID_EXPORT_WARNING",
                severity="medium",
                title="周上网售电异常",
                description=f"本周累计新能源盈余{total_surplus:.2f}kWh，但售电收入几乎为0。请检查并网配置。",
                data={"total_surplus_kwh": total_surplus, "total_feed_in_revenue": total_feedin}
            ))

        return suggestions
