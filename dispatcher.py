from datetime import datetime
from typing import Dict, Any, Tuple, Optional
import config
from models import MicrogridState, DispatchDecision, AuditLog
from demand_response import DemandResponseManager
from price_forecast import PriceForecastManager
from audit import AuditBuilder, AnomalyDetector


class DispatchEngine:
    def __init__(self, state: MicrogridState, dr_manager: Optional[DemandResponseManager] = None,
                 price_forecast_manager: Optional[PriceForecastManager] = None):
        self.state = state
        self.dr_manager = dr_manager
        self.price_forecast_manager = price_forecast_manager
        self.anomaly_detector = AnomalyDetector(state)

    def _get_time_interval_hours(self, now: datetime) -> float:
        if self.state.last_dispatch_time is None:
            return config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0
        delta = (now - self.state.last_dispatch_time).total_seconds() / 3600.0
        return max(delta, config.DEFAULT_DISPATCH_INTERVAL_MINUTES / 60.0)

    def execute(self, now: datetime = None) -> DispatchDecision:
        if now is None:
            now = datetime.now()

        if not self.state.all_sources_reported():
            missing = []
            for sid in config.PV_CONFIG:
                if sid not in self.state.pv_reports:
                    missing.append(f"pv:{sid}")
            for sid in config.WT_CONFIG:
                if sid not in self.state.wt_reports:
                    missing.append(f"wt:{sid}")
            if self.state.load_report is None:
                missing.append("load")
            raise ValueError(f"缺少上报数据: {', '.join(missing)}")

        dispatch_id = self.state.generate_dispatch_id()
        audit_builder = AuditBuilder(self.state, dispatch_id, now)

        has_active_price_strategy = (self.price_forecast_manager is not None and 
                                     self.price_forecast_manager.get_active_strategy() is not None)
        
        if self.state.should_generate_plan(now) and not has_active_price_strategy:
            self.state.generate_storage_plan(now)
            audit_builder.add_branch(
                "储能计划生成",
                True,
                "到达计划生成时间，生成当日储能充放电计划",
                {"plan_date": now.strftime("%Y-%m-%d")}
            )
        elif has_active_price_strategy:
            audit_builder.add_branch(
                "储能计划生成",
                True,
                "使用价格预测策略生成的储能计划",
                {"plan_date": now.strftime("%Y-%m-%d"), "from_price_strategy": True}
            )
        else:
            audit_builder.add_branch(
                "储能计划生成",
                False,
                "未到计划生成时间或当日计划已生成"
            )

        time_interval_hours = self._get_time_interval_hours(now)

        total_renewable = self.state.get_total_renewable_kw()
        load_kw = self.state.get_load_kw()

        pv_output = {}
        for sid in config.PV_CONFIG:
            r = self.state.pv_reports.get(sid)
            pv_output[sid] = max(0.0, r.power_kw) if (r and r.available) else 0.0

        wt_output = {}
        for sid in config.WT_CONFIG:
            r = self.state.wt_reports.get(sid)
            wt_output[sid] = max(0.0, r.power_kw) if (r and r.available) else 0.0

        diesel_output = {}
        for ds_id in config.DIESEL_CONFIG:
            diesel_output[ds_id] = 0.0

        bes_id = list(config.BESS_CONFIG.keys())[0]
        ds_id = list(config.DIESEL_CONFIG.keys())[0]

        soc_abnormal = self.state.check_and_handle_soc_abnormal(bes_id, now)
        if soc_abnormal:
            audit_builder.add_branch(
                "电池SOC异常处理",
                True,
                "电池SOC低于下限，暂停当前时段储能计划",
                {"current_soc": self.state.bess_state[bes_id].soc,
                 "soc_min": config.BESS_CONFIG[bes_id]["soc_min"]}
            )
        else:
            audit_builder.add_branch(
                "电池SOC异常处理",
                False,
                "电池SOC在正常区间"
            )

        bess_action = {}
        for bid in config.BESS_CONFIG:
            bess_action[bid] = {"charge_kw": 0.0, "discharge_kw": 0.0, "soc_before": self.state.bess_state[bid].soc}

        grid_import_kw = 0.0
        grid_export_kw = 0.0
        load_shed_kw = 0.0
        notes = []
        total_cost = 0.0
        arbitrage_charge_kwh = 0.0
        arbitrage_charge_cost = 0.0
        arbitrage_discharge_kwh = 0.0
        arbitrage_discharge_revenue = 0.0
        load_grid_import_kwh = 0.0

        tariff_period = config.get_tariff_period(now.hour)
        diesel_gen_cost = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"]
        feed_in_price = config.FEED_IN_TARIFF

        force_discharge = False
        dynamic_price_active = False
        if self.price_forecast_manager:
            self.price_forecast_manager.check_and_expire_strategy(now)
            original_grid_buy_price = config.GRID_TARIFF[tariff_period]["price"]
            grid_buy_price = self.price_forecast_manager.get_effective_buy_price(now.hour)
            force_discharge = self.price_forecast_manager.is_force_discharge_hour(now.hour)
            price_source = "forecast"
            if abs(grid_buy_price - original_grid_buy_price) > 0.001:
                dynamic_price_active = True
                notes.append(f"动态电价生效：预测电价{grid_buy_price:.2f}元/kWh，固定电价{original_grid_buy_price:.2f}元/kWh")
        else:
            grid_buy_price = config.GRID_TARIFF[tariff_period]["price"]
            price_source = "fixed"

        audit_builder.add_branch(
            "电价策略判定",
            True,
            f"当前时段为{tariff_period}，购电价{grid_buy_price}元/kWh，上网电价{feed_in_price}元/kWh",
            {"tariff_period": tariff_period, "grid_buy_price": grid_buy_price,
             "feed_in_price": feed_in_price, "price_source": price_source,
             "force_discharge": force_discharge, "dynamic_price_active": dynamic_price_active}
        )

        if force_discharge:
            notes.append("高价风险时段：电价预测强制放电，禁止购电，优先使用电池放电")
            audit_builder.add_branch(
                "强制放电模式",
                True,
                "电价预测触发强制放电，禁止购电",
                {"hour": now.hour, "grid_buy_price": grid_buy_price}
            )

        current_hour_plan = self.state.get_current_hour_plan(now)
        storage_mode = "normal"
        if current_hour_plan and current_hour_plan.active and not current_hour_plan.abnormal:
            storage_mode = current_hour_plan.mode
            storage_mode_cn = {"active_charge": "谷时主动充电", "priority_discharge": "峰时优先放电", "normal": "常规模式"}.get(storage_mode, storage_mode)
            if storage_mode != "normal":
                notes.append(f"储能策略模式: {storage_mode_cn}")
            audit_builder.add_branch(
                "储能策略模式",
                True,
                f"储能计划生效，当前模式：{storage_mode_cn}",
                {"mode": storage_mode, "mode_chinese": storage_mode_cn,
                 "plan_active": current_hour_plan.active,
                 "plan_abnormal": current_hour_plan.abnormal}
            )
        else:
            audit_builder.add_branch(
                "储能策略模式",
                False,
                "无生效储能计划，采用常规模式",
                {"mode": "normal"}
            )

        backup_plans = self.state.get_active_backup_plans()
        if backup_plans:
            audit_builder.add_branch(
                "故障预案触发",
                True,
                f"当前有{len(backup_plans)}个活跃故障预案",
                {"plan_ids": [p.plan_id for p in backup_plans],
                 "affected_sources": [f"{p.source_type}:{p.source_id}" for p in backup_plans]}
            )
        else:
            audit_builder.add_branch(
                "故障预案触发",
                False,
                "当前无活跃故障预案"
            )

        remaining_load = load_kw - total_renewable

        if storage_mode == "active_charge":
            cfg = config.BESS_CONFIG[bes_id]
            energy_space = (cfg["soc_max"] - self.state.bess_state[bes_id].soc) * cfg["capacity_kwh"]
            max_charge_by_space = energy_space / cfg["charge_efficiency"] / time_interval_hours
            max_charge = self.state.get_bess_max_charge_with_health(bes_id, time_interval_hours)
            target_charge_kw = min(cfg["max_charge_power"], max_charge_by_space, max_charge)

            renewable_for_load = min(total_renewable, load_kw)
            renewable_surplus = max(0.0, total_renewable - load_kw)

            charge_from_renewable = min(renewable_surplus, target_charge_kw)
            remaining_charge_target = target_charge_kw - charge_from_renewable

            if renewable_for_load > 0:
                notes.append(f"[主动充电] 新能源供负荷 {renewable_for_load:.2f}kW")

            if charge_from_renewable > 0:
                bess_action[bes_id]["charge_kw"] = charge_from_renewable
                notes.append(f"[主动充电] 新能源盈余充电 {charge_from_renewable:.2f}kW")
                audit_builder.add_branch(
                    "电池充电决策",
                    True,
                    f"使用新能源盈余充电{charge_from_renewable:.2f}kW",
                    {"charge_kw": charge_from_renewable, "source": "renewable_surplus"}
                )

            remaining_load = load_kw - renewable_for_load

            if remaining_load > 0:
                use_grid = (grid_buy_price < diesel_gen_cost) and not force_discharge
                diesel_cap = self.state.get_available_diesel_capacity(ds_id, now)

                if use_grid:
                    grid_import_load = remaining_load
                    remaining_load = 0
                    load_cost = grid_import_load * time_interval_hours * grid_buy_price
                    total_cost += load_cost
                    grid_import_kw += grid_import_load
                    load_grid_import_kwh += grid_import_load * time_interval_hours
                    notes.append(f"[主动充电] 谷时购电供负荷 {grid_import_load:.2f}kW")
                    audit_builder.add_branch(
                        "购电决策",
                        True,
                        f"谷时电价({grid_buy_price}元)低于柴油成本({diesel_gen_cost}元)，选择购电供负荷",
                        {"grid_import_kw": grid_import_load, "reason": "grid_cheaper_than_diesel"}
                    )
                    audit_builder.add_branch(
                        "柴油机启动决策",
                        False,
                        "购电成本更低，不启动柴油机",
                        {"grid_price": grid_buy_price, "diesel_cost": diesel_gen_cost}
                    )
                elif force_discharge:
                    audit_builder.add_branch(
                        "购电决策",
                        False,
                        "强制放电模式，禁止购电",
                        {"reason": "force_discharge_mode"}
                    )
                else:
                    audit_builder.add_branch(
                        "购电决策",
                        False,
                        f"谷时电价({grid_buy_price}元)高于柴油成本({diesel_gen_cost}元)，不选择购电",
                        {"reason": "diesel_cheaper_than_grid"}
                    )
                    if diesel_cap["can_run"]:
                        diesel_kw = min(remaining_load, diesel_cap["max_output"])
                        diesel_output[ds_id] = diesel_kw
                        remaining_load -= diesel_kw
                        self.state.diesel_state[ds_id].output_kw = diesel_kw
                        total_cost += diesel_kw * time_interval_hours * diesel_gen_cost
                        if diesel_cap.get("startup_cost_applies"):
                            startup_cost = config.DIESEL_CONFIG[ds_id]["startup_cost"]
                            total_cost += startup_cost
                            self.state.start_diesel(ds_id, now)
                            notes.append(f"启动柴油机 (固定成本 {startup_cost}元)，出力 {diesel_kw:.2f}kW")
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                True,
                                f"购电成本高于柴油，启动柴油机，出力{diesel_kw:.2f}kW",
                                {"diesel_output_kw": diesel_kw, "startup_cost": startup_cost,
                                 "reason": "diesel_cheaper_than_grid"}
                            )
                        else:
                            notes.append(f"柴油机恢复/持续运行，出力 {diesel_kw:.2f}kW")
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                True,
                                f"柴油机已运行，持续出力{diesel_kw:.2f}kW",
                                {"diesel_output_kw": diesel_kw, "already_running": True}
                            )
                    else:
                        audit_builder.add_branch(
                            "柴油机启动决策",
                            False,
                            f"柴油机不可用: {diesel_cap.get('reason', '未知原因')}",
                            {"reason": diesel_cap.get("reason", "unknown")}
                        )

            can_charge_from_grid = (remaining_load <= 0) or dynamic_price_active
            if remaining_charge_target > 0 and can_charge_from_grid and grid_buy_price < diesel_gen_cost:
                grid_for_charge = min(remaining_charge_target, cfg["max_charge_power"] - bess_action[bes_id]["charge_kw"])
                if grid_for_charge > 0:
                    bess_action[bes_id]["charge_kw"] += grid_for_charge
                    grid_import_kw += grid_for_charge
                    charge_cost = grid_for_charge * time_interval_hours * grid_buy_price
                    total_cost += charge_cost
                    arbitrage_charge_kwh += grid_for_charge * time_interval_hours
                    arbitrage_charge_cost += charge_cost
                    charge_note = f"[主动充电] 谷时购电充电 {grid_for_charge:.2f}kW (成本 {charge_cost:.4f}元)"
                    if dynamic_price_active:
                        charge_note = f"[主动充电] 动态电价购电充电 {grid_for_charge:.2f}kW (成本 {charge_cost:.4f}元)"
                    notes.append(charge_note)
                    audit_builder.add_branch(
                        "电池充电决策",
                        True,
                        f"电价套利，购电充电{grid_for_charge:.2f}kW",
                        {"charge_kw": grid_for_charge, "source": "grid",
                         "cost": charge_cost, "reason": "price_arbitrage",
                         "dynamic_price": dynamic_price_active}
                    )

            if remaining_load > 0:
                load_shed_kw = remaining_load
                total_available = renewable_for_load + diesel_output[ds_id] + (grid_import_kw - bess_action[bes_id]["charge_kw"] + charge_from_renewable)
                self.state.add_alert(
                    "LOAD_SHEDDING",
                    f"负荷缺口 {load_shed_kw:.2f}kW，执行甩负荷",
                    {"load_kw": load_kw, "total_available_kw": total_available, "shed_kw": load_shed_kw}
                )
                notes.append(f"警告：甩负荷 {load_shed_kw:.2f}kW")
                audit_builder.add_branch(
                    "甩负荷决策",
                    True,
                    f"供电能力不足，甩负荷{load_shed_kw:.2f}kW",
                    {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                     "load_kw": load_kw}
                )
            else:
                audit_builder.add_branch(
                    "甩负荷决策",
                    False,
                    "供电能力充足，无需甩负荷"
                )

        elif storage_mode == "priority_discharge":
            if remaining_load > 0:
                cfg_b = config.BESS_CONFIG[bes_id]
                base_max_discharge = self.state.get_bess_max_discharge(bes_id, time_interval_hours)
                max_discharge = base_max_discharge

                discharge_kw = min(remaining_load, max_discharge)
                if discharge_kw > 0:
                    bess_action[bes_id]["discharge_kw"] = discharge_kw
                    remaining_load -= discharge_kw
                    peak_price = config.get_peak_price()
                    saved_cost = discharge_kw * time_interval_hours * peak_price
                    arbitrage_discharge_kwh += discharge_kw * time_interval_hours
                    arbitrage_discharge_revenue += saved_cost
                    notes.append(f"[优先放电] 峰时段电池放电 {discharge_kw:.2f}kW (避免购电，等效收益 {saved_cost:.4f}元)")
                    audit_builder.add_branch(
                        "电池放电决策",
                        True,
                        f"峰时段优先放电{discharge_kw:.2f}kW，避免高价购电",
                        {"discharge_kw": discharge_kw, "saved_cost": saved_cost,
                         "reason": "peak_hour_priority_discharge"}
                    )

                    self.state._update_health_percent(bes_id)
                    bh = self.state.bess_state[bes_id].health
                    if bh.health_percent < cfg_b["health_derating_threshold"]:
                        notes.append(f"注: 峰时段优先放电，放电功率不降额使用 {max_discharge:.1f}kW (健康度{bh.health_percent:.1f}%)")
                else:
                    audit_builder.add_branch(
                        "电池放电决策",
                        False,
                        "电池SOC已达下限或无放电需求",
                        {"soc": self.state.bess_state[bes_id].soc,
                         "soc_min": cfg_b["soc_min"]}
                    )

                if remaining_load > 0:
                    use_grid = grid_buy_price < diesel_gen_cost
                    diesel_cap = self.state.get_available_diesel_capacity(ds_id, now)

                    if use_grid:
                        grid_import_load = remaining_load
                        remaining_load = 0
                        load_cost = grid_import_load * time_interval_hours * grid_buy_price
                        total_cost += load_cost
                        grid_import_kw += grid_import_load
                        load_grid_import_kwh += grid_import_load * time_interval_hours
                        notes.append(f"外购电供负荷 {grid_import_load:.2f}kW (电价 {grid_buy_price:.2f}元/kWh)")
                        audit_builder.add_branch(
                            "购电决策",
                            True,
                            f"电价({grid_buy_price}元)低于柴油成本({diesel_gen_cost}元)，选择购电",
                            {"grid_import_kw": grid_import_load, "reason": "grid_cheaper_than_diesel"}
                        )
                        audit_builder.add_branch(
                            "柴油机启动决策",
                            False,
                            "购电成本更低，不启动柴油机",
                            {"grid_price": grid_buy_price, "diesel_cost": diesel_gen_cost}
                        )
                    else:
                        audit_builder.add_branch(
                            "购电决策",
                            False,
                            f"电价({grid_buy_price}元)高于柴油成本({diesel_gen_cost}元)，不选择购电",
                            {"reason": "diesel_cheaper_than_grid"}
                        )
                        if diesel_cap["can_run"]:
                            diesel_kw = min(remaining_load, diesel_cap["max_output"])
                            diesel_output[ds_id] = diesel_kw
                            remaining_load -= diesel_kw
                            self.state.diesel_state[ds_id].output_kw = diesel_kw
                            total_cost += diesel_kw * time_interval_hours * diesel_gen_cost
                            if diesel_cap.get("startup_cost_applies"):
                                startup_cost = config.DIESEL_CONFIG[ds_id]["startup_cost"]
                                total_cost += startup_cost
                                self.state.start_diesel(ds_id, now)
                                notes.append(f"启动柴油机 (固定成本 {startup_cost}元)，出力 {diesel_kw:.2f}kW")
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    True,
                                    f"购电成本高于柴油，启动柴油机，出力{diesel_kw:.2f}kW",
                                    {"diesel_output_kw": diesel_kw, "startup_cost": startup_cost,
                                     "reason": "diesel_cheaper_than_grid"}
                                )
                            else:
                                notes.append(f"柴油机恢复/持续运行，出力 {diesel_kw:.2f}kW")
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    True,
                                    f"柴油机已运行，持续出力{diesel_kw:.2f}kW",
                                    {"diesel_output_kw": diesel_kw, "already_running": True}
                                )
                        else:
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                False,
                                f"柴油机不可用: {diesel_cap.get('reason', '未知原因')}",
                                {"reason": diesel_cap.get("reason", "unknown")}
                            )

                if remaining_load > 0:
                    load_shed_kw = remaining_load
                    total_available = total_renewable + bess_action[bes_id]["discharge_kw"] + diesel_output[ds_id] + grid_import_kw
                    self.state.add_alert(
                        "LOAD_SHEDDING",
                        f"负荷缺口 {load_shed_kw:.2f}kW，执行甩负荷",
                        {"load_kw": load_kw, "total_available_kw": total_available, "shed_kw": load_shed_kw}
                    )
                    notes.append(f"警告：甩负荷 {load_shed_kw:.2f}kW")
                    audit_builder.add_branch(
                        "甩负荷决策",
                        True,
                        f"供电能力不足，甩负荷{load_shed_kw:.2f}kW",
                        {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                         "load_kw": load_kw}
                    )
                else:
                    audit_builder.add_branch(
                        "甩负荷决策",
                        False,
                        "供电能力充足，无需甩负荷"
                    )

            elif remaining_load < 0:
                surplus = -remaining_load
                max_charge = self.state.get_bess_max_charge_with_health(bes_id, time_interval_hours)
                charge_kw = min(surplus, max_charge)
                if charge_kw > 0:
                    bess_action[bes_id]["charge_kw"] = charge_kw
                    surplus -= charge_kw
                    notes.append(f"电池充电 {charge_kw:.2f}kW (新能源盈余)")
                    audit_builder.add_branch(
                        "电池充电决策",
                        True,
                        f"新能源盈余充电{charge_kw:.2f}kW",
                        {"charge_kw": charge_kw, "source": "renewable_surplus"}
                    )

                    self.state._update_health_percent(bes_id)
                    bh = self.state.bess_state[bes_id].health
                    cfg_b = config.BESS_CONFIG[bes_id]
                    if bh.health_percent < cfg_b["health_derating_threshold"]:
                        notes.append(f"电池健康度{bh.health_percent:.1f}% < {cfg_b['health_derating_threshold']}%，已降额{cfg_b['power_derating_ratio']*100:.0f}%运行")
                else:
                    audit_builder.add_branch(
                        "电池充电决策",
                        False,
                        "电池SOC已达上限或无充电需求"
                    )

                if surplus > 0:
                    grid_export_kw = surplus
                    surplus = 0
                    revenue = grid_export_kw * time_interval_hours * feed_in_price
                    total_cost -= revenue
                    notes.append(f"余电上网 {grid_export_kw:.2f}kW (收入 {revenue:.2f}元)")

                audit_builder.add_branch(
                    "电池放电决策",
                    False,
                    "新能源出力大于负荷，无需放电"
                )
                audit_builder.add_branch(
                    "购电决策",
                    False,
                    "新能源出力充足，无需购电"
                )
                audit_builder.add_branch(
                    "柴油机启动决策",
                    False,
                    "新能源出力充足，无需启动柴油机"
                )
                audit_builder.add_branch(
                    "甩负荷决策",
                    False,
                    "供电能力充足，无需甩负荷"
                )

            else:
                audit_builder.add_branch(
                    "电池放电决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需放电"
                )
                audit_builder.add_branch(
                    "购电决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需购电"
                )
                audit_builder.add_branch(
                    "柴油机启动决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需启动柴油机"
                )
                audit_builder.add_branch(
                    "甩负荷决策",
                    False,
                    "供电能力充足，无需甩负荷"
                )

        else:
            if remaining_load > 0:
                max_discharge = self.state.get_bess_max_discharge_with_health(bes_id, time_interval_hours)
                discharge_kw = min(remaining_load, max_discharge)
                if discharge_kw > 0:
                    bess_action[bes_id]["discharge_kw"] = discharge_kw
                    remaining_load -= discharge_kw
                    notes.append(f"电池放电 {discharge_kw:.2f}kW")
                    audit_builder.add_branch(
                        "电池放电决策",
                        True,
                        f"负荷缺口{discharge_kw + remaining_load:.2f}kW，电池放电填补{discharge_kw:.2f}kW",
                        {"discharge_kw": discharge_kw, "remaining_load_before": remaining_load + discharge_kw,
                         "remaining_load_after": remaining_load}
                    )

                    self.state._update_health_percent(bes_id)
                    bh = self.state.bess_state[bes_id].health
                    cfg_b = config.BESS_CONFIG[bes_id]
                    if bh.health_percent < cfg_b["health_derating_threshold"]:
                        notes.append(f"电池健康度{bh.health_percent:.1f}% < {cfg_b['health_derating_threshold']}%，已降额{cfg_b['power_derating_ratio']*100:.0f}%运行")
                else:
                    audit_builder.add_branch(
                        "电池放电决策",
                        False,
                        "电池SOC已达下限或无放电能力",
                        {"soc": self.state.bess_state[bes_id].soc,
                         "max_discharge_kw": max_discharge}
                    )

                if remaining_load > 0:
                    use_grid = grid_buy_price < diesel_gen_cost
                    diesel_cap = self.state.get_available_diesel_capacity(ds_id, now)

                    if use_grid:
                        grid_import_load = remaining_load
                        remaining_load = 0
                        load_cost = grid_import_load * time_interval_hours * grid_buy_price
                        total_cost += load_cost
                        grid_import_kw += grid_import_load
                        load_grid_import_kwh += grid_import_load * time_interval_hours
                        notes.append(f"外购电 {grid_import_load:.2f}kW (电价 {grid_buy_price:.2f}元/kWh，低于柴油成本)")
                        audit_builder.add_branch(
                            "购电决策",
                            True,
                            f"电价({grid_buy_price}元)低于柴油成本({diesel_gen_cost}元)，选择购电填补剩余缺口",
                            {"grid_import_kw": grid_import_load, "reason": "grid_cheaper_than_diesel"}
                        )
                        audit_builder.add_branch(
                            "柴油机启动决策",
                            False,
                            "购电成本更低，不启动柴油机",
                            {"grid_price": grid_buy_price, "diesel_cost": diesel_gen_cost}
                        )
                    else:
                        audit_builder.add_branch(
                            "购电决策",
                            False,
                            f"电价({grid_buy_price}元)高于柴油成本({diesel_gen_cost}元)，不选择购电",
                            {"reason": "diesel_cheaper_than_grid"}
                        )
                        if diesel_cap["can_run"]:
                            diesel_kw = min(remaining_load, diesel_cap["max_output"])
                            diesel_output[ds_id] = diesel_kw
                            remaining_load -= diesel_kw
                            self.state.diesel_state[ds_id].output_kw = diesel_kw

                            total_cost += diesel_kw * time_interval_hours * diesel_gen_cost

                            if diesel_cap.get("startup_cost_applies"):
                                startup_cost = config.DIESEL_CONFIG[ds_id]["startup_cost"]
                                total_cost += startup_cost
                                self.state.start_diesel(ds_id, now)
                                notes.append(f"启动柴油机 (固定成本 {startup_cost}元)，出力 {diesel_kw:.2f}kW")
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    True,
                                    f"购电成本高于柴油，启动柴油机填补缺口，出力{diesel_kw:.2f}kW",
                                    {"diesel_output_kw": diesel_kw, "startup_cost": startup_cost,
                                     "reason": "diesel_cheaper_than_grid"}
                                )
                            else:
                                notes.append(f"柴油机恢复/持续运行，出力 {diesel_kw:.2f}kW")
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    True,
                                    f"柴油机已运行，持续出力{diesel_kw:.2f}kW",
                                    {"diesel_output_kw": diesel_kw, "already_running": True}
                                )
                        else:
                            notes.append(f"柴油机不可用: {diesel_cap.get('reason', '未知原因')}，且购电价({grid_buy_price})高于柴油成本({diesel_gen_cost})，不选择买电")
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                False,
                                f"柴油机不可用: {diesel_cap.get('reason', '未知原因')}",
                                {"reason": diesel_cap.get("reason", "unknown")}
                            )

                    if remaining_load > 0:
                        load_shed_kw = remaining_load
                        total_available = total_renewable + bess_action[bes_id]["discharge_kw"] + diesel_output[ds_id] + grid_import_kw
                        self.state.add_alert(
                            "LOAD_SHEDDING",
                            f"负荷缺口 {load_shed_kw:.2f}kW，执行甩负荷",
                            {"load_kw": load_kw, "total_available_kw": total_available, "shed_kw": load_shed_kw}
                        )
                        notes.append(f"警告：甩负荷 {load_shed_kw:.2f}kW (本地所有可用源出力不足)")
                        audit_builder.add_branch(
                            "甩负荷决策",
                            True,
                            f"所有可用源仍无法覆盖负荷，甩负荷{load_shed_kw:.2f}kW",
                            {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                             "load_kw": load_kw}
                        )
                    else:
                        audit_builder.add_branch(
                            "甩负荷决策",
                            False,
                            "供电能力充足，无需甩负荷"
                        )
                else:
                    audit_builder.add_branch(
                        "购电决策",
                        False,
                        "电池放电已覆盖剩余负荷，无需购电"
                    )
                    audit_builder.add_branch(
                        "柴油机启动决策",
                        False,
                        "电池放电已覆盖剩余负荷，无需启动柴油机"
                    )
                    audit_builder.add_branch(
                        "甩负荷决策",
                        False,
                        "供电能力充足，无需甩负荷"
                    )

            elif remaining_load < 0:
                surplus = -remaining_load
                max_charge = self.state.get_bess_max_charge_with_health(bes_id, time_interval_hours)
                charge_kw = min(surplus, max_charge)
                if charge_kw > 0:
                    bess_action[bes_id]["charge_kw"] = charge_kw
                    surplus -= charge_kw
                    notes.append(f"电池充电 {charge_kw:.2f}kW")
                    audit_builder.add_branch(
                        "电池充电决策",
                        True,
                        f"新能源盈余充电{charge_kw:.2f}kW",
                        {"charge_kw": charge_kw, "source": "renewable_surplus"}
                    )

                    self.state._update_health_percent(bes_id)
                    bh = self.state.bess_state[bes_id].health
                    cfg_b = config.BESS_CONFIG[bes_id]
                    if bh.health_percent < cfg_b["health_derating_threshold"]:
                        notes.append(f"电池健康度{bh.health_percent:.1f}% < {cfg_b['health_derating_threshold']}%，已降额{cfg_b['power_derating_ratio']*100:.0f}%运行")
                else:
                    audit_builder.add_branch(
                        "电池充电决策",
                        False,
                        "电池SOC已达上限或无充电需求"
                    )

                if surplus > 0:
                    grid_export_kw = surplus
                    surplus = 0
                    revenue = grid_export_kw * time_interval_hours * feed_in_price
                    total_cost -= revenue
                    notes.append(f"余电上网 {grid_export_kw:.2f}kW (收入 {revenue:.2f}元)")

                audit_builder.add_branch(
                    "电池放电决策",
                    False,
                    "新能源出力大于负荷，无需放电"
                )
                audit_builder.add_branch(
                    "购电决策",
                    False,
                    "新能源出力充足，无需购电"
                )
                audit_builder.add_branch(
                    "柴油机启动决策",
                    False,
                    "新能源出力充足，无需启动柴油机"
                )
                audit_builder.add_branch(
                    "甩负荷决策",
                    False,
                    "供电能力充足，无需甩负荷"
                )

            else:
                audit_builder.add_branch(
                    "电池放电决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需放电"
                )
                audit_builder.add_branch(
                    "购电决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需购电"
                )
                audit_builder.add_branch(
                    "柴油机启动决策",
                    False,
                    "新能源出力刚好覆盖负荷，无需启动柴油机"
                )
                audit_builder.add_branch(
                    "甩负荷决策",
                    False,
                    "供电能力充足，无需甩负荷"
                )

        ds = self.state.diesel_state[ds_id]
        diesel_cfg = config.DIESEL_CONFIG[ds_id]
        if ds.running and diesel_output[ds_id] == 0:
            if ds.last_start_time:
                elapsed = (now - ds.last_start_time).total_seconds() / 60.0
            else:
                elapsed = 0
            if elapsed >= diesel_cfg["min_runtime_minutes"]:
                self.state.stop_diesel(ds_id, now)
                notes.append(f"柴油机满足最小运行时间，正常停机")
                audit_builder.add_branch(
                    "柴油机停机决策",
                    True,
                    f"柴油机空载运行{elapsed:.1f}分钟，满足最小运行时间，正常停机",
                    {"runtime_minutes": elapsed, "min_runtime_minutes": diesel_cfg["min_runtime_minutes"]}
                )
            else:
                diesel_output[ds_id] = 0.0
                notes.append(f"柴油机空载维持运行 (已运行 {elapsed:.1f}分钟，需满 {diesel_cfg['min_runtime_minutes']}分钟才能停机)")
                self.state.diesel_state[ds_id].output_kw = 0.0
                audit_builder.add_branch(
                    "柴油机停机决策",
                    False,
                    f"柴油机空载运行{elapsed:.1f}分钟，未满足最小运行时间{diesel_cfg['min_runtime_minutes']}分钟，维持空载",
                    {"runtime_minutes": elapsed, "min_runtime_minutes": diesel_cfg["min_runtime_minutes"]}
                )

        soc_before_dispatch = bess_action[bes_id]["soc_before"]
        self.state.update_bess_soc(
            bes_id,
            bess_action[bes_id]["charge_kw"],
            bess_action[bes_id]["discharge_kw"],
            time_interval_hours
        )
        bess_action[bes_id]["soc_after"] = self.state.bess_state[bes_id].soc

        if bess_action[bes_id]["discharge_kw"] > 0:
            self.state.record_bess_discharge(
                bes_id,
                bess_action[bes_id]["discharge_kw"],
                soc_before_dispatch,
                bess_action[bes_id]["soc_after"],
                time_interval_hours
            )
            self.state._update_cycle_count(
                bes_id,
                bess_action[bes_id]["discharge_kw"],
                time_interval_hours
            )

        if bess_action[bes_id]["charge_kw"] > 0:
            self.state.record_bess_charge(
                bes_id,
                bess_action[bes_id]["charge_kw"],
                time_interval_hours
            )

        if arbitrage_charge_kwh > 0:
            self.state.record_arbitrage_charge(arbitrage_charge_kwh, arbitrage_charge_cost)
        if arbitrage_discharge_kwh > 0:
            self.state.record_arbitrage_discharge(arbitrage_discharge_kwh, arbitrage_discharge_revenue)

        for sid, kw in pv_output.items():
            self.state.stats.total_pv_generated_kwh[sid] += kw * time_interval_hours
        for sid, kw in wt_output.items():
            self.state.stats.total_wt_generated_kwh[sid] += kw * time_interval_hours

        diesel_kwh = diesel_output[ds_id] * time_interval_hours
        self.state.diesel_state[ds_id].total_generated_kwh += diesel_kwh
        self.state.stats.total_diesel_generated_kwh += diesel_kwh

        self.state.stats.total_grid_import_kwh += grid_import_kw * time_interval_hours
        self.state.stats.total_grid_export_kwh += grid_export_kw * time_interval_hours
        self.state.stats.load_grid_import_kwh += load_grid_import_kwh
        self.state.stats.total_cost += total_cost
        self.state.stats.total_load_shed_kwh += load_shed_kw * time_interval_hours

        load_served = load_kw - load_shed_kw

        decision = DispatchDecision(
            timestamp=now,
            pv_output=pv_output,
            wt_output=wt_output,
            diesel_output=diesel_output,
            bess_action=bess_action,
            grid_import_kw=grid_import_kw,
            grid_export_kw=grid_export_kw,
            load_served_kw=load_served,
            load_shed_kw=load_shed_kw,
            cost=total_cost,
            tariff_period=tariff_period,
            grid_buy_price=grid_buy_price,
            notes=notes,
        )

        if self.dr_manager:
            self.dr_manager.start_event_if_due(now)
            self.dr_manager.check_and_finish_events(now)
            decision = self.dr_manager.apply_dr_constraints(decision, now)

        self.state.add_dispatch(decision)

        audit_id = self.state.generate_audit_id()
        audit_log = audit_builder.build(decision, audit_id)

        anomalies = self.anomaly_detector.detect_all(audit_log)
        audit_log.anomalies = anomalies

        self.state.add_audit_log(audit_log)

        return decision
