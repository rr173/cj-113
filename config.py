from datetime import datetime

# ============================================
# 微网设备配置
# ============================================

# ============================================
# 分时段储能策略配置
# ============================================
STORAGE_STRATEGY_CONFIG = {
    "plan_generation_hour": 0,
    "plan_generation_minute": 0,
    "min_arbitrage_profit_ratio": 0.05,
    "enable_strategy": True,
}


PV_CONFIG = {
    "pv1": {"rated_power": 100.0, "name": "光伏阵列1"},
    "pv2": {"rated_power": 100.0, "name": "光伏阵列2"},
}

WT_CONFIG = {
    "wt1": {"rated_power": 50.0, "name": "风机1"},
}

DIESEL_CONFIG = {
    "ds1": {
        "rated_power": 200.0,
        "name": "柴油发电机1",
        "startup_cost": 50.0,
        "generation_cost": 2.0,
        "min_runtime_minutes": 30,
        "cooldown_minutes": 15,
    }
}

BESS_CONFIG = {
    "bes1": {
        "capacity_kwh": 500.0,
        "max_charge_power": 100.0,
        "max_discharge_power": 100.0,
        "soc_min": 0.20,
        "soc_max": 0.90,
        "initial_soc": 0.50,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.92,
        "name": "电池储能系统1",
        "cycle_life_threshold": 3000,
        "health_derating_threshold": 30,
        "power_derating_ratio": 0.20,
        "internal_resistance_degradation_ratio": 0.15,
        "baseline_discharge_count": 100,
        "recent_discharge_count": 30,
        "max_discharge_records": 500,
    }
}

GRID_TARIFF = {
    "valley": {"price": 0.4, "hours": [(23, 7)]},
    "flat": {"price": 0.8, "hours": [(7, 11), (15, 23)]},
    "peak": {"price": 1.2, "hours": [(11, 15)]},
}

FEED_IN_TARIFF = 0.3

DEFAULT_DISPATCH_INTERVAL_MINUTES = 1

LOAD_GROUP_CONFIG = {
    "group1": {
        "name": "一级(关键负荷)",
        "description": "消防系统、安防监控、数据中心UPS",
        "rated_power_kw": 50.0,
        "max_shed_ratio": 0.0,
        "shed_priority": 1,
        "restore_priority": 1,
    },
    "group2": {
        "name": "二级(重要负荷)",
        "description": "办公照明、电梯、通风",
        "rated_power_kw": 120.0,
        "max_shed_ratio": 0.6,
        "shed_priority": 2,
        "restore_priority": 2,
    },
    "group3": {
        "name": "三级(一般负荷)",
        "description": "空调、充电桩、景观照明",
        "rated_power_kw": 180.0,
        "max_shed_ratio": 1.0,
        "shed_priority": 3,
        "restore_priority": 3,
    },
}


def get_tariff_period(current_hour: int) -> str:
    for period, info in GRID_TARIFF.items():
        for start, end in info["hours"]:
            if start < end:
                if start <= current_hour < end:
                    return period
            else:
                if current_hour >= start or current_hour < end:
                    return period
    return "flat"


def get_grid_buy_price(now: datetime = None) -> float:
    if now is None:
        now = datetime.now()
    period = get_tariff_period(now.hour)
    return GRID_TARIFF[period]["price"]


def get_soc_limits():
    cfg = BESS_CONFIG["bes1"]
    return cfg["soc_min"], cfg["soc_max"]


def get_storage_mode_for_hour(hour: int) -> str:
    period = get_tariff_period(hour)
    if period == "valley":
        return "active_charge"
    elif period == "peak":
        return "priority_discharge"
    else:
        return "normal"


def get_valley_price() -> float:
    return GRID_TARIFF["valley"]["price"]


def get_peak_price() -> float:
    return GRID_TARIFF["peak"]["price"]


def get_flat_price() -> float:
    return GRID_TARIFF["flat"]["price"]


COST_ATTRIBUTION_CONFIG = {
    "load_shed_penalty_per_kwh": 3.0,
    "missed_opportunity_soc_target": 0.90,
}


DYNAMIC_SHED_CONFIG = {
    "enable_dynamic_shed": True,
    "pressure_history_size": 50,
    "shed_history_window": 5,
    "mode_switch_history_size": 50,
    "relaxed_mode_threshold": 30,
    "emergency_mode_threshold": 70,
    "relaxed_group3_ratio_multiplier": 0.5,
    "relaxed_group2_ratio_multiplier": 0.0,
    "normal_group3_ratio_multiplier": 1.0,
    "normal_group2_ratio_multiplier": 1.0,
    "emergency_group3_ratio_multiplier": 1.0,
    "emergency_group2_ratio_multiplier": 1.5,
    "emergency_group2_max_ratio": 1.0,
    "emergency_forced_shed_soc_threshold": 0.25,
    "restore_rate_per_cycle": 0.3,
    "score_low_soc": 30,
    "score_low_soc_threshold": 0.30,
    "score_low_renewable": 20,
    "score_low_renewable_ratio": 0.50,
    "score_diesel_running": 15,
    "score_peak_hour": 10,
    "score_recent_shed": 25,
}
