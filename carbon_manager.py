from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config
from models import CarbonEmissionRecord, CarbonQuotaState


class CarbonManager:
    def __init__(self, state):
        self.state = state
        self._carbon_record_counter: int = 0
        self.carbon_emission_records: List[CarbonEmissionRecord] = []
        self.quota_state: Optional[CarbonQuotaState] = None
        self._init_quota_state()

    def _init_quota_state(self):
        now = datetime.now()
        current_month = now.strftime("%Y-%m")
        quota_kg = config.CARBON_CONFIG["monthly_quota_kg"]
        warning_threshold = quota_kg * config.CARBON_CONFIG["warning_threshold_ratio"]
        emergency_threshold = quota_kg * config.CARBON_CONFIG["emergency_threshold_ratio"]

        self.quota_state = CarbonQuotaState(
            current_month=current_month,
            monthly_quota_kg=quota_kg,
            accumulated_emission_kg=0.0,
            current_status="normal",
            warning_threshold_kg=warning_threshold,
            emergency_threshold_kg=emergency_threshold,
            last_reset_time=now,
            daily_emissions={},
        )

    def _check_monthly_reset(self, now: datetime):
        current_month = now.strftime("%Y-%m")
        if current_month != self.quota_state.current_month:
            self.quota_state.current_month = current_month
            self.quota_state.accumulated_emission_kg = 0.0
            self.quota_state.current_status = "normal"
            self.quota_state.daily_emissions = {}
            self.quota_state.last_reset_time = now

    def _update_status(self):
        accumulated = self.quota_state.accumulated_emission_kg
        quota = self.quota_state.monthly_quota_kg

        if accumulated >= quota:
            self.quota_state.current_status = "exceeded"
        elif accumulated >= self.quota_state.emergency_threshold_kg:
            self.quota_state.current_status = "emergency"
        elif accumulated >= self.quota_state.warning_threshold_kg:
            self.quota_state.current_status = "warning"
        else:
            self.quota_state.current_status = "normal"

    def calculate_emission(self, diesel_kwh: float, grid_kwh: float) -> Tuple[float, float, float]:
        diesel_emission = diesel_kwh * config.CARBON_CONFIG["diesel_emission_factor"]
        grid_emission = grid_kwh * config.CARBON_CONFIG["grid_emission_factor"]
        total_emission = diesel_emission + grid_emission
        return diesel_emission, grid_emission, total_emission

    def record_emission(self, dispatch_id: str, diesel_kwh: float, grid_kwh: float,
                        now: datetime = None) -> CarbonEmissionRecord:
        if now is None:
            now = datetime.now()

        if not config.CARBON_CONFIG["enable_carbon_tracking"]:
            return None

        self._check_monthly_reset(now)

        diesel_emission, grid_emission, total_emission = self.calculate_emission(diesel_kwh, grid_kwh)

        self.quota_state.accumulated_emission_kg += total_emission

        date_key = now.strftime("%Y-%m-%d")
        if date_key not in self.quota_state.daily_emissions:
            self.quota_state.daily_emissions[date_key] = 0.0
        self.quota_state.daily_emissions[date_key] += total_emission

        old_status = self.quota_state.current_status
        self._update_status()
        new_status = self.quota_state.current_status

        quota_remaining_ratio = max(0.0, 1.0 - self.quota_state.accumulated_emission_kg / self.quota_state.monthly_quota_kg)

        self._carbon_record_counter += 1
        record_id = f"CARB-{self._carbon_record_counter:08d}"

        record = CarbonEmissionRecord(
            record_id=record_id,
            dispatch_id=dispatch_id,
            timestamp=now,
            diesel_emission_kg=round(diesel_emission, 4),
            grid_emission_kg=round(grid_emission, 4),
            total_emission_kg=round(total_emission, 4),
            diesel_generated_kwh=round(diesel_kwh, 4),
            grid_import_kwh=round(grid_kwh, 4),
            carbon_status=new_status,
            quota_remaining_ratio=round(quota_remaining_ratio, 4),
        )

        self.carbon_emission_records.append(record)

        if old_status != new_status:
            self.state.add_alert(
                f"CARBON_STATUS_{new_status.upper()}",
                f"碳排放状态变更: {old_status} → {new_status}，累计排放 {self.quota_state.accumulated_emission_kg:.2f}kgCO2，配额 {self.quota_state.monthly_quota_kg:.2f}kgCO2",
                {
                    "old_status": old_status,
                    "new_status": new_status,
                    "accumulated_emission_kg": self.quota_state.accumulated_emission_kg,
                    "monthly_quota_kg": self.quota_state.monthly_quota_kg,
                    "quota_used_ratio": round(1 - quota_remaining_ratio, 4),
                }
            )

        return record

    def get_current_quota_state(self) -> Dict[str, any]:
        accumulated = self.quota_state.accumulated_emission_kg
        quota = self.quota_state.monthly_quota_kg
        used_ratio = accumulated / quota if quota > 0 else 0.0
        remaining_ratio = max(0.0, 1.0 - used_ratio)

        status_map = {
            "normal": "正常",
            "warning": "碳预警",
            "emergency": "碳紧急",
            "exceeded": "碳超标",
        }

        return {
            "current_month": self.quota_state.current_month,
            "monthly_quota_kg": round(quota, 2),
            "accumulated_emission_kg": round(accumulated, 2),
            "used_ratio": round(used_ratio, 4),
            "remaining_ratio": round(remaining_ratio, 4),
            "current_status": self.quota_state.current_status,
            "current_status_chinese": status_map.get(self.quota_state.current_status, "未知"),
            "warning_threshold_kg": round(self.quota_state.warning_threshold_kg, 2),
            "emergency_threshold_kg": round(self.quota_state.emergency_threshold_kg, 2),
            "last_reset_time": self.quota_state.last_reset_time.isoformat() if self.quota_state.last_reset_time else None,
        }

    def get_emission_records(self, start_time: datetime = None, end_time: datetime = None,
                             limit: int = 100, offset: int = 0) -> List[CarbonEmissionRecord]:
        records = []
        for r in self.carbon_emission_records:
            if start_time and r.timestamp < start_time:
                continue
            if end_time and r.timestamp > end_time:
                continue
            records.append(r)

        result = list(reversed(records))
        total = len(result)
        start_idx = offset
        end_idx = min(offset + limit, total)
        return result[start_idx:end_idx]

    def get_daily_trend(self, start_date: str = None, end_date: str = None) -> List[Dict[str, Any]]:
        daily = self.quota_state.daily_emissions

        result = []
        for date_key in sorted(daily.keys()):
            if start_date and date_key < start_date:
                continue
            if end_date and date_key > end_date:
                continue
            result.append({
                "date": date_key,
                "total_emission_kg": round(daily[date_key], 4),
            })

        return result

    def update_monthly_quota(self, new_quota_kg: float) -> bool:
        if new_quota_kg <= 0:
            return False

        self.quota_state.monthly_quota_kg = new_quota_kg
        self.quota_state.warning_threshold_kg = new_quota_kg * config.CARBON_CONFIG["warning_threshold_ratio"]
        self.quota_state.emergency_threshold_kg = new_quota_kg * config.CARBON_CONFIG["emergency_threshold_ratio"]
        self._update_status()

        config.CARBON_CONFIG["monthly_quota_kg"] = new_quota_kg
        return True

    def reset_monthly_accumulated(self, add_quota_kg: float = 0.0) -> bool:
        now = datetime.now()
        self.quota_state.accumulated_emission_kg = 0.0
        self.quota_state.last_reset_time = now

        if add_quota_kg > 0:
            self.quota_state.monthly_quota_kg += add_quota_kg
            self.quota_state.warning_threshold_kg = self.quota_state.monthly_quota_kg * config.CARBON_CONFIG["warning_threshold_ratio"]
            self.quota_state.emergency_threshold_kg = self.quota_state.monthly_quota_kg * config.CARBON_CONFIG["emergency_threshold_ratio"]
            config.CARBON_CONFIG["monthly_quota_kg"] = self.quota_state.monthly_quota_kg

        self._update_status()
        return True

    def get_carbon_status_for_dispatch(self) -> Dict[str, Any]:
        status = self.quota_state.current_status
        accumulated = self.quota_state.accumulated_emission_kg
        quota = self.quota_state.monthly_quota_kg
        remaining_ratio = max(0.0, 1.0 - accumulated / quota)

        return {
            "status": status,
            "accumulated_emission_kg": accumulated,
            "monthly_quota_kg": quota,
            "remaining_ratio": remaining_ratio,
            "is_warning": status == "warning",
            "is_emergency": status == "emergency",
            "is_exceeded": status == "exceeded",
            "emergency_grid_limit_ratio": config.CARBON_CONFIG["emergency_grid_limit_ratio"],
            "exceed_penalty_multiplier": config.CARBON_CONFIG["exceed_penalty_multiplier"],
        }

    def calculate_exceed_penalty(self, total_emission_kg: float, grid_buy_price: float) -> float:
        if self.quota_state.current_status != "exceeded":
            return 0.0

        accumulated = self.quota_state.accumulated_emission_kg
        quota = self.quota_state.monthly_quota_kg
        exceed_amount = max(0.0, accumulated - quota)

        if exceed_amount <= 0:
            return 0.0

        penalty_per_kg = grid_buy_price * config.CARBON_CONFIG["exceed_penalty_multiplier"]
        penalty = total_emission_kg * penalty_per_kg

        return penalty
