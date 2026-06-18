from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import uuid

import config
from models import (
    PriceForecastRecord,
    PriceComparisonHour,
    PriceComparisonResult,
    StrategySuggestionHour,
    PurchaseStrategy,
    StrategyExecutionDayStats,
    StrategyExecutionStatsSummary,
    MicrogridState,
)


class PriceForecastManager:
    VALLEY_OPPORTUNITY_THRESHOLD_RATIO = 0.0
    PEAK_RISK_THRESHOLD_RATIO = 0.5
    FORCE_DISCHARGE_PRICE_MULTIPLIER = 1.5

    def __init__(self, state: MicrogridState):
        self.state = state
        self._forecasts: Dict[str, PriceForecastRecord] = {}
        self._comparisons: Dict[str, PriceComparisonResult] = {}
        self._strategies: Dict[str, PurchaseStrategy] = {}
        self._forecasts_by_date: Dict[str, str] = {}
        self._counter = 0
        self._active_strategy_id: Optional[str] = None
        self._execution_stats: Dict[str, StrategyExecutionDayStats] = {}

    def submit_forecast(self, prices: List[float], forecast_date: str = None) -> Dict[str, Any]:
        if len(prices) != 24:
            raise ValueError("必须提供24个时段的电价数据")

        for i, p in enumerate(prices):
            if p < 0:
                raise ValueError(f"第{i}小时电价不能为负数")

        if forecast_date is None:
            forecast_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        if forecast_date in self._forecasts_by_date:
            old_forecast_id = self._forecasts_by_date[forecast_date]
            old_forecast = self._forecasts.get(old_forecast_id)
            if old_forecast and old_forecast.status == "active":
                self.deactivate_strategy(old_forecast_id)
            if old_forecast_id in self._forecasts:
                self._forecasts[old_forecast_id].status = "expired"
            if old_forecast_id in self._strategies:
                self._strategies[old_forecast_id].status = "expired"

        self._counter += 1
        forecast_id = f"FCST-{self._counter:06d}"

        forecast = PriceForecastRecord(
            forecast_id=forecast_id,
            forecast_date=forecast_date,
            prices=prices.copy(),
            submitted_at=datetime.now(),
            status="pending",
        )

        self._forecasts[forecast_id] = forecast
        self._forecasts_by_date[forecast_date] = forecast_id

        comparison = self._generate_comparison(forecast)
        self._comparisons[forecast_id] = comparison

        strategy = self._generate_strategy(forecast, comparison)
        self._strategies[strategy.strategy_id] = strategy

        return {
            "forecast": forecast,
            "comparison": comparison,
            "strategy": strategy,
        }

    def _generate_comparison(self, forecast: PriceForecastRecord) -> PriceComparisonResult:
        valley_price = config.get_valley_price()
        peak_price = config.get_peak_price()
        valley_threshold = valley_price * (1 + self.VALLEY_OPPORTUNITY_THRESHOLD_RATIO)
        peak_threshold = peak_price * (1 + self.PEAK_RISK_THRESHOLD_RATIO)

        hours: List[PriceComparisonHour] = []
        valley_hours: List[int] = []
        peak_hours: List[int] = []
        total_valley_savings = 0.0
        total_peak_risk_cost = 0.0

        for hour in range(24):
            forecast_price = forecast.prices[hour]
            fixed_period = config.get_tariff_period(hour)
            fixed_price = config.GRID_TARIFF[fixed_period]["price"]

            price_diff = forecast_price - fixed_price
            price_diff_ratio = price_diff / fixed_price if fixed_price > 0 else 0.0

            is_valley_opportunity = forecast_price <= valley_threshold
            is_peak_risk = forecast_price >= peak_threshold

            if is_valley_opportunity:
                valley_hours.append(hour)
                total_valley_savings += (fixed_price - forecast_price)

            if is_peak_risk:
                peak_hours.append(hour)
                total_peak_risk_cost += (forecast_price - fixed_price)

            hours.append(PriceComparisonHour(
                hour=hour,
                forecast_price=forecast_price,
                fixed_price=fixed_price,
                fixed_period=fixed_period,
                price_diff=price_diff,
                price_diff_ratio=price_diff_ratio,
                is_valley_opportunity=is_valley_opportunity,
                is_peak_risk=is_peak_risk,
            ))

        return PriceComparisonResult(
            forecast_id=forecast.forecast_id,
            forecast_date=forecast.forecast_date,
            hours=hours,
            valley_opportunity_hours=valley_hours,
            peak_risk_hours=peak_hours,
            total_valley_savings_potential=total_valley_savings,
            total_peak_risk_cost=total_peak_risk_cost,
            valley_price_threshold=valley_threshold,
            peak_price_threshold=peak_threshold,
        )

    def _generate_strategy(self, forecast: PriceForecastRecord,
                           comparison: PriceComparisonResult) -> PurchaseStrategy:
        strategy_id = f"STRAT-{self._counter:06d}"
        valley_price = config.get_valley_price()
        peak_price = config.get_peak_price()
        force_discharge_threshold = peak_price * self.FORCE_DISCHARGE_PRICE_MULTIPLIER

        hours: List[StrategySuggestionHour] = []
        active_charge_hours = 0
        priority_discharge_hours = 0
        force_discharge_hours = 0
        normal_hours = 0

        for hour in range(24):
            forecast_price = forecast.prices[hour]
            fixed_period = config.get_tariff_period(hour)
            fixed_price = config.GRID_TARIFF[fixed_period]["price"]

            if forecast_price <= valley_price:
                action = "active_charge"
                reason = f"预告电价({forecast_price:.2f})低于固定谷时电价({valley_price:.2f})，建议主动充电"
                active_charge_hours += 1
            elif forecast_price >= force_discharge_threshold:
                action = "force_discharge_no_grid"
                reason = f"预告电价({forecast_price:.2f})超过固定峰时电价1.5倍({force_discharge_threshold:.2f})，强制放电禁止购电"
                force_discharge_hours += 1
            elif forecast_price >= peak_price:
                action = "priority_discharge"
                reason = f"预告电价({forecast_price:.2f})高于等于固定峰时电价({peak_price:.2f})，建议优先放电"
                priority_discharge_hours += 1
            else:
                action = "normal"
                reason = f"预告电价({forecast_price:.2f})处于正常区间，按常规模式运行"
                normal_hours += 1

            hours.append(StrategySuggestionHour(
                hour=hour,
                suggested_action=action,
                reason=reason,
                forecast_price=forecast_price,
                fixed_price=fixed_price,
            ))

        summary = {
            "active_charge_hours": active_charge_hours,
            "priority_discharge_hours": priority_discharge_hours,
            "force_discharge_hours": force_discharge_hours,
            "normal_hours": normal_hours,
            "valley_price": valley_price,
            "peak_price": peak_price,
            "force_discharge_threshold": force_discharge_threshold,
            "estimated_savings_per_kwh": comparison.total_valley_savings_potential / max(1, active_charge_hours),
        }

        return PurchaseStrategy(
            strategy_id=strategy_id,
            forecast_id=forecast.forecast_id,
            forecast_date=forecast.forecast_date,
            generated_at=datetime.now(),
            status="pending",
            hours=hours,
            summary=summary,
        )

    def get_forecast(self, forecast_id: str) -> Optional[PriceForecastRecord]:
        return self._forecasts.get(forecast_id)

    def get_forecast_by_date(self, date_str: str) -> Optional[PriceForecastRecord]:
        forecast_id = self._forecasts_by_date.get(date_str)
        if forecast_id:
            return self._forecasts.get(forecast_id)
        return None

    def get_comparison(self, forecast_id: str) -> Optional[PriceComparisonResult]:
        return self._comparisons.get(forecast_id)

    def get_strategy(self, strategy_id: str) -> Optional[PurchaseStrategy]:
        return self._strategies.get(strategy_id)

    def get_strategy_by_forecast(self, forecast_id: str) -> Optional[PurchaseStrategy]:
        for s in self._strategies.values():
            if s.forecast_id == forecast_id:
                return s
        return None

    def activate_strategy(self, forecast_id: str) -> bool:
        forecast = self._forecasts.get(forecast_id)
        if not forecast:
            return False

        if forecast.status not in ("pending", "deactivated"):
            return False

        strategy = self.get_strategy_by_forecast(forecast_id)
        if not strategy:
            return False

        if self._active_strategy_id and self._active_strategy_id != strategy.strategy_id:
            old_strategy = self._strategies.get(self._active_strategy_id)
            if old_strategy:
                old_strategy.status = "deactivated"
                old_forecast = self._forecasts.get(old_strategy.forecast_id)
                if old_forecast:
                    old_forecast.status = "deactivated"
                    old_forecast.deactivated_at = datetime.now()

        forecast.status = "active"
        forecast.activated_at = datetime.now()
        strategy.status = "active"
        self._active_strategy_id = strategy.strategy_id

        self._update_storage_plan_with_strategy(strategy)

        return True

    def deactivate_strategy(self, forecast_id: str) -> bool:
        forecast = self._forecasts.get(forecast_id)
        if not forecast:
            return False

        if forecast.status != "active":
            return False

        strategy = self.get_strategy_by_forecast(forecast_id)
        if not strategy:
            return False

        forecast.status = "deactivated"
        forecast.deactivated_at = datetime.now()
        strategy.status = "deactivated"

        if self._active_strategy_id == strategy.strategy_id:
            self._active_strategy_id = None

        self._restore_default_storage_plan()

        return True

    def _update_storage_plan_with_strategy(self, strategy: PurchaseStrategy):
        from models import StoragePlan, StoragePlanHour

        plan_date = strategy.forecast_date
        plan = StoragePlan(plan_date=plan_date, generated_at=datetime.now())

        for s_hour in strategy.hours:
            if s_hour.suggested_action == "active_charge":
                mode = "active_charge"
            elif s_hour.suggested_action in ("priority_discharge", "force_discharge_no_grid"):
                mode = "priority_discharge"
            else:
                mode = "normal"

            period = config.get_tariff_period(s_hour.hour)

            plan.hours[s_hour.hour] = StoragePlanHour(
                hour=s_hour.hour,
                mode=mode,
                tariff_period=period,
                active=True,
                abnormal=False,
            )

        self.state.current_storage_plan = plan
        self.state.last_plan_generation_date = plan_date

    def _restore_default_storage_plan(self):
        self.state.generate_storage_plan(datetime.now())

    def get_active_strategy(self) -> Optional[PurchaseStrategy]:
        if not self._active_strategy_id:
            return None
        return self._strategies.get(self._active_strategy_id)

    def get_active_forecast(self) -> Optional[PriceForecastRecord]:
        strategy = self.get_active_strategy()
        if not strategy:
            return None
        return self._forecasts.get(strategy.forecast_id)

    def is_force_discharge_hour(self, hour: int) -> bool:
        strategy = self.get_active_strategy()
        if not strategy:
            return False

        for s_hour in strategy.hours:
            if s_hour.hour == hour:
                return s_hour.suggested_action == "force_discharge_no_grid"
        return False

    def get_effective_buy_price(self, hour: int) -> float:
        forecast = self.get_active_forecast()
        if forecast and 0 <= hour < 24:
            return forecast.prices[hour]
        period = config.get_tariff_period(hour)
        return config.GRID_TARIFF[period]["price"]

    def list_forecasts(self, limit: int = 30) -> List[PriceForecastRecord]:
        forecasts = sorted(self._forecasts.values(), key=lambda f: f.submitted_at, reverse=True)
        return forecasts[:limit]

    def check_and_expire_strategy(self, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()

        strategy = self.get_active_strategy()
        if not strategy:
            return False

        today_str = now.strftime("%Y-%m-%d")
        if strategy.forecast_date < today_str:
            self.deactivate_strategy(strategy.forecast_id)
            forecast = self._forecasts.get(strategy.forecast_id)
            if forecast:
                forecast.status = "expired"
            strategy.status = "expired"
            return True

        return False

    def record_execution_stats(self, date_str: str, strategy_used: bool,
                                strategy_id: Optional[str],
                                grid_import_kwh: float, buy_cost: float,
                                load_served_kwh: float, load_shed_kwh: float = 0.0):
        avg_price = buy_cost / grid_import_kwh if grid_import_kwh > 0 else 0.0

        stats = StrategyExecutionDayStats(
            date=date_str,
            strategy_used=strategy_used,
            strategy_id=strategy_id,
            avg_buy_price=avg_price,
            total_grid_import_kwh=grid_import_kwh,
            total_buy_cost=buy_cost,
            total_load_served_kwh=load_served_kwh,
            total_load_shed_kwh=load_shed_kwh,
        )

        self._execution_stats[date_str] = stats

    def get_execution_stats(self, start_date: str = None, end_date: str = None) -> StrategyExecutionStatsSummary:
        all_dates = sorted(self._execution_stats.keys())

        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        strategy_days_list: List[StrategyExecutionDayStats] = []
        no_strategy_days_list: List[StrategyExecutionDayStats] = []

        for d in all_dates:
            stats = self._execution_stats[d]
            if stats.strategy_used:
                strategy_days_list.append(stats)
            else:
                no_strategy_days_list.append(stats)

        avg_with_strategy = (
            sum(s.total_buy_cost for s in strategy_days_list) / len(strategy_days_list)
            if strategy_days_list else 0.0
        )
        avg_without_strategy = (
            sum(s.total_buy_cost for s in no_strategy_days_list) / len(no_strategy_days_list)
            if no_strategy_days_list else 0.0
        )

        if avg_without_strategy > 0:
            cost_saving_ratio = (avg_without_strategy - avg_with_strategy) / avg_without_strategy
        else:
            cost_saving_ratio = 0.0

        total_saving = sum(
            (s.total_buy_cost - avg_without_strategy) for s in strategy_days_list
        ) if strategy_days_list and no_strategy_days_list else 0.0

        details = [self._execution_stats[d] for d in all_dates]

        return StrategyExecutionStatsSummary(
            strategy_days=len(strategy_days_list),
            no_strategy_days=len(no_strategy_days_list),
            avg_cost_with_strategy=avg_with_strategy,
            avg_cost_without_strategy=avg_without_strategy,
            cost_saving_ratio=cost_saving_ratio,
            total_saving=total_saving,
            details=details,
        )

    def get_hour_strategy(self, hour: int) -> Optional[StrategySuggestionHour]:
        strategy = self.get_active_strategy()
        if not strategy:
            return None
        for s_hour in strategy.hours:
            if s_hour.hour == hour:
                return s_hour
        return None
