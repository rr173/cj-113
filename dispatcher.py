from datetime import datetime
from typing import Dict, Any, Tuple, Optional
from copy import deepcopy
import config
from models import MicrogridState, DispatchDecision, AuditLog, StrategyParams
from demand_response import DemandResponseManager
from price_forecast import PriceForecastManager
from audit import AuditBuilder, AnomalyDetector, CostAttributionAnalyzer
from island_manager import OperationMode


class DispatchEngine:
    def __init__(self, state: MicrogridState, dr_manager: Optional[DemandResponseManager] = None,
                 price_forecast_manager: Optional[PriceForecastManager] = None,
                 strategy_params: Optional[StrategyParams] = None):
        self.state = state
        self.dr_manager = dr_manager
        self.price_forecast_manager = price_forecast_manager
        self.anomaly_detector = AnomalyDetector(state)
        if strategy_params is None:
            strategy_params = StrategyParams()
        self.strategy_params = strategy_params

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

        maintenance_update = self.state.check_and_update_maintenance_plans(now)

        ds_manager = self.state.dual_strategy_manager
        dual_enabled = ds_manager.enable
        shadow_state_copy = None
        if dual_enabled:
            self.strategy_params = ds_manager.main_strategy
            shadow_state_copy = deepcopy(self.state)

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
            restriction = self.state.get_device_maintenance_restriction("pv", sid)
            if restriction:
                if restriction.handling_mode == "full_shutdown":
                    pv_output[sid] = 0.0
                else:
                    base_pv = max(0.0, r.power_kw) if (r and r.available) else 0.0
                    pv_output[sid] = base_pv * (1.0 - restriction.derating_percent / 100.0)
            else:
                pv_output[sid] = max(0.0, r.power_kw) if (r and r.available) else 0.0

        wt_output = {}
        for sid in config.WT_CONFIG:
            r = self.state.wt_reports.get(sid)
            restriction = self.state.get_device_maintenance_restriction("wt", sid)
            if restriction:
                if restriction.handling_mode == "full_shutdown":
                    wt_output[sid] = 0.0
                else:
                    base_wt = max(0.0, r.power_kw) if (r and r.available) else 0.0
                    wt_output[sid] = base_wt * (1.0 - restriction.derating_percent / 100.0)
            else:
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
        diesel_startup_occurred = False

        for plan_info in maintenance_update.get("started", []):
            type_cn = {
                "routine_inspection": "日常巡检",
                "preventive_maintenance": "预防性维护",
                "fault_repair": "故障抢修",
            }
            mode_cn = {
                "full_shutdown": "完全停用",
                "derated": f"降额{plan_info['derating_percent']}%运行",
            }
            device_cn = {"pv": "光伏", "wt": "风电", "diesel": "柴油机", "bess": "储能电池"}
            msg = (f"[维保开始] 计划{plan_info['plan_id']}：{device_cn.get(plan_info['device_type'], plan_info['device_type'])}"
                   f"设备{plan_info['device_id']}进入维保状态，处理方式：{mode_cn.get(plan_info['handling_mode'], plan_info['handling_mode'])}，"
                   f"维保类型：{type_cn.get(plan_info.get('maintenance_type', ''), plan_info.get('maintenance_type', ''))}")
            if plan_info.get("is_bess"):
                msg += f"，SOC限制：{round(plan_info['soc_min_override']*100, 1)}%~{round(plan_info['soc_max_override']*100, 1)}%"
            notes.append(msg)
        for plan_info in maintenance_update.get("ended", []):
            device_cn = {"pv": "光伏", "wt": "风电", "diesel": "柴油机", "bess": "储能电池"}
            notes.append(
                f"[维保结束] 计划{plan_info['plan_id']}：{device_cn.get(plan_info['device_type'], plan_info['device_type'])}"
                f"设备{plan_info['device_id']}维保结束，已恢复正常运行"
            )

        active_restrictions = self.state.get_current_maintenance_restrictions()
        bess_maintenance_disabled = set()
        if active_restrictions["total_affected"] > 0:
            for r in active_restrictions["restrictions"]:
                if r["handling_mode"] == "full_shutdown" and r["device_type"] == "bess":
                    bess_maintenance_disabled.add(r["device_id"])
                elif r["device_type"] in ("pv", "wt"):
                    notes.append(
                        f"[维保留效] {r['device_type_chinese']}{r['device_id']}：{r['handling_mode_chinese']}"
                        + (f"（降额{r['derating_percent']}%）" if r["handling_mode"] == "derated" else "")
                    )
                elif r["device_type"] == "diesel":
                    notes.append(
                        f"[维保留效] {r['device_type_chinese']}{r['device_id']}：{r['handling_mode_chinese']}"
                        + (f"（降额{r['derating_percent']}%）" if r["handling_mode"] == "derated" else "")
                    )
                elif r["device_type"] == "bess":
                    soc_info = f"，SOC限制{r['soc_min_percent']}%~{r['soc_max_percent']}%"
                    notes.append(
                        f"[维保留效] {r['device_type_chinese']}{r['device_id']}：{r['handling_mode_chinese']}"
                        + (f"（降额{r['derating_percent']}%）" if r["handling_mode"] == "derated" else "")
                        + soc_info
                    )
        arbitrage_charge_kwh = 0.0
        arbitrage_charge_cost = 0.0
        arbitrage_discharge_kwh = 0.0
        arbitrage_discharge_revenue = 0.0
        load_grid_import_kwh = 0.0

        group_shed_details: Dict[str, Dict[str, Any]] = {}
        group_restore_details: Dict[str, Dict[str, Any]] = {}

        prev_active_shed_count = sum(1 for g in self.state._active_shed_events.values() if g.shed_power_kw > 0.01)

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

        island_mode = self.state.island_manager.mode in (OperationMode.ISLAND, OperationMode.BLACK_START)
        island_grid_blocked = False
        if island_mode:
            island_grid_blocked = True
            force_discharge = True
            island_mgr = self.state.island_manager
            mode_cn = island_mgr.get_mode_name()
            notes.append(f"[{mode_cn}] 孤岛运行，禁止购电和售电，SOC下限{island_mgr.get_island_soc_min()*100:.0f}%")
            audit_builder.add_branch(
                "孤岛模式",
                True,
                f"{mode_cn}，禁止购电和售电，SOC下限提升至{island_mgr.get_island_soc_min()*100:.0f}%",
                {"mode": island_mgr.mode.value, "mode_chinese": mode_cn,
                 "soc_min": island_mgr.get_island_soc_min()}
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

        dynamic_shed_enabled = config.DYNAMIC_SHED_CONFIG.get("enable_dynamic_shed", False)
        if dynamic_shed_enabled:
            pressure_index = self.state.compute_power_pressure_index(now)
            new_mode, mode_changed, old_mode = self.state.update_shed_mode(now, pressure_index)
            mode_cn = {"relaxed": "宽松模式", "normal": "正常模式", "emergency": "紧急模式"}.get(new_mode, new_mode)
            notes.append(f"供电压力指数: {pressure_index:.1f}，当前模式: {mode_cn}")
            if mode_changed:
                old_mode_cn = {"relaxed": "宽松模式", "normal": "正常模式", "emergency": "紧急模式"}.get(old_mode, old_mode)
                notes.append(f"模式切换: {old_mode_cn} → {mode_cn} (压力指数{pressure_index:.1f})")
            audit_builder.add_branch(
                "动态限额模式",
                True,
                f"供电压力指数{pressure_index:.1f}，当前模式: {mode_cn}",
                {"pressure_index": pressure_index, "mode": new_mode, "mode_chinese": mode_cn,
                 "mode_changed": mode_changed, "old_mode": old_mode if mode_changed else None,
                 "manual_lock": self.state.shed_mode_manual_lock}
            )
        else:
            audit_builder.add_branch(
                "动态限额模式",
                False,
                "动态限额功能未启用",
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

        carbon_enabled = config.CARBON_CONFIG.get("enable_carbon_tracking", False)
        carbon_status_info = None
        if carbon_enabled:
            carbon_status_info = self.state.carbon_manager.get_carbon_status_for_dispatch()
            carbon_status = carbon_status_info["status"]
            remaining_ratio = carbon_status_info["remaining_ratio"]
            status_cn = {
                "normal": "正常",
                "warning": "碳预警",
                "emergency": "碳紧急",
                "exceeded": "碳超标"
            }.get(carbon_status, "未知")
            notes.append(f"碳排放状态: {status_cn}，剩余配额 {remaining_ratio*100:.1f}%")
            audit_builder.add_branch(
                "碳排放配额状态",
                True,
                f"当前状态: {status_cn}，累计排放 {carbon_status_info['accumulated_emission_kg']:.2f}kgCO2，配额 {carbon_status_info['monthly_quota_kg']:.2f}kgCO2",
                {
                    "carbon_status": carbon_status,
                    "carbon_status_chinese": status_cn,
                    "accumulated_emission_kg": carbon_status_info["accumulated_emission_kg"],
                    "monthly_quota_kg": carbon_status_info["monthly_quota_kg"],
                    "remaining_ratio": remaining_ratio,
                }
            )
        else:
            audit_builder.add_branch(
                "碳排放配额状态",
                False,
                "碳排放追踪功能未启用",
                {"carbon_status": "disabled"}
            )

        remaining_load = load_kw - total_renewable

        carbon_shed_occurred = False
        carbon_grid_limit_applied = False

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
                    carbon_emergency = False
                    if carbon_enabled and carbon_status_info:
                        if carbon_status_info["status"] in ("emergency", "exceeded"):
                            carbon_emergency = True

                    if carbon_emergency:
                        grid_limit_ratio = config.CARBON_CONFIG["emergency_grid_limit_ratio"]
                        grid_import_load = remaining_load * grid_limit_ratio
                        remaining_after_grid = remaining_load - grid_import_load
                        carbon_shed_occurred = True

                        load_cost = grid_import_load * time_interval_hours * grid_buy_price
                        total_cost += load_cost
                        grid_import_kw += grid_import_load
                        load_grid_import_kwh += grid_import_load * time_interval_hours
                        remaining_load = remaining_after_grid

                        notes.append(
                            f"[碳紧急] 购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                            f"购电{grid_import_load:.2f}kW，剩余{remaining_after_grid:.2f}kW走甩负荷"
                        )
                        audit_builder.add_branch(
                            "购电决策",
                            True,
                            f"碳紧急状态，购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                            f"购电{grid_import_load:.2f}kW",
                            {"grid_import_kw": grid_import_load, "reason": "carbon_emergency_limit",
                             "limit_ratio": grid_limit_ratio}
                        )
                        audit_builder.add_branch(
                            "柴油机启动决策",
                            False,
                            "碳紧急状态，禁止启动柴油机",
                            {"reason": "carbon_emergency_prohibition"}
                        )
                    else:
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
                    carbon_can_start_diesel = True
                    carbon_reason = ""
                    if carbon_enabled and carbon_status_info:
                        if carbon_status_info["is_emergency"] or carbon_status_info["is_exceeded"]:
                            carbon_can_start_diesel = False
                            carbon_reason = "碳紧急/超标状态，禁止启动柴油机"
                        elif carbon_status_info["is_warning"]:
                            if not force_discharge:
                                carbon_can_start_diesel = False
                                carbon_reason = "碳预警状态，禁止经济性柴油机启动（购电可满足负荷）"
                            else:
                                carbon_can_start_diesel = True
                                carbon_reason = "碳预警状态，强制放电模式下不启动柴油机会甩负荷，允许启动"

                    if carbon_enabled and not carbon_can_start_diesel:
                        is_emergency = carbon_status_info["status"] in ("emergency", "exceeded")
                        if not force_discharge:
                            if is_emergency:
                                grid_limit_ratio = config.CARBON_CONFIG["emergency_grid_limit_ratio"]
                                grid_import_load = remaining_load * grid_limit_ratio
                                remaining_load_after = remaining_load - grid_import_load
                                carbon_shed_occurred = True

                                load_cost = grid_import_load * time_interval_hours * grid_buy_price
                                total_cost += load_cost
                                grid_import_kw += grid_import_load
                                load_grid_import_kwh += grid_import_load * time_interval_hours
                                remaining_load = remaining_load_after

                                notes.append(
                                    f"[碳紧急] 购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                                    f"购电{grid_import_load:.2f}kW，剩余{remaining_load_after:.2f}kW走甩负荷"
                                )
                                audit_builder.add_branch(
                                    "购电决策",
                                    True,
                                    f"碳紧急状态，购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                                    f"购电{grid_import_load:.2f}kW",
                                    {"grid_import_kw": grid_import_load, "reason": "carbon_emergency_limit",
                                     "limit_ratio": grid_limit_ratio}
                                )
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    False,
                                    carbon_reason,
                                    {"reason": "carbon_constraint", "carbon_status": carbon_status_info["status"]}
                                )
                            else:
                                grid_import_load = remaining_load
                                remaining_load_after = 0
                                load_cost = grid_import_load * time_interval_hours * grid_buy_price
                                total_cost += load_cost
                                grid_import_kw += grid_import_load
                                load_grid_import_kwh += grid_import_load * time_interval_hours
                                notes.append(f"[碳约束] {carbon_reason}，改用购电 {grid_import_load:.2f}kW")
                                audit_builder.add_branch(
                                    "购电决策",
                                    True,
                                    f"{carbon_reason}，强制购电 {grid_import_load:.2f}kW",
                                    {"grid_import_kw": grid_import_load, "reason": "carbon_constraint",
                                     "carbon_status": carbon_status_info["status"]}
                                )
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    False,
                                    carbon_reason,
                                    {"reason": "carbon_constraint", "carbon_status": carbon_status_info["status"]}
                                )
                                remaining_load = remaining_load_after
                        else:
                            notes.append(f"[碳约束] {carbon_reason}，且强制放电模式禁止购电")
                            audit_builder.add_branch(
                                "购电决策",
                                False,
                                "强制放电模式禁止购电",
                                {"reason": "force_discharge_mode"}
                            )
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                False,
                                carbon_reason,
                                {"reason": "carbon_constraint", "carbon_status": carbon_status_info["status"]}
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
                                diesel_startup_occurred = True
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
                shed_threshold = self.strategy_params.shed_trigger_threshold_ratio
                gap_ratio = remaining_load / load_kw if load_kw > 0 else 1.0
                if gap_ratio <= shed_threshold and shed_threshold > 0:
                    grid_fill = remaining_load
                    grid_fill_cost = grid_fill * time_interval_hours * grid_buy_price
                    total_cost += grid_fill_cost
                    grid_import_kw += grid_fill
                    load_grid_import_kwh += grid_fill * time_interval_hours
                    remaining_load = 0
                    notes.append(f"[甩负荷容忍带] 缺口占比{gap_ratio*100:.1f}% ≤ 阈值{shed_threshold*100:.0f}%，强制购电填补{grid_fill:.2f}kW (成本{grid_fill_cost:.4f}元)")
                    audit_builder.add_branch(
                        "甩负荷决策",
                        False,
                        f"缺口占比{gap_ratio*100:.1f}%低于阈值{shed_threshold*100:.0f}%，容忍带内强制购电填补",
                        {"gap_ratio": gap_ratio, "threshold": shed_threshold, "grid_fill_kw": grid_fill, "reason": "shed_tolerance_band"}
                    )
                    shed_result = {}
                    unshed_gap = 0.0
                    total_shed_actual = 0
                    load_shed_kw = 0.0
                else:
                    total_available = renewable_for_load + diesel_output[ds_id] + (grid_import_kw - bess_action[bes_id]["charge_kw"] + charge_from_renewable)
                    shed_result, unshed_gap = self.state.compute_priority_load_shedding_dynamic(
                        remaining_load, now, dispatch_id
                    )
                    total_shed_actual = sum(shed_result.values())
                    load_shed_kw = total_shed_actual + unshed_gap

                    shed_breakdown = []
                    for gid, kw in shed_result.items():
                        gcfg = config.LOAD_GROUP_CONFIG[gid]
                        shed_breakdown.append(f"{gcfg['name']}切{kw:.2f}kW")
                        group_shed_details[gid] = {
                            "group_id": gid,
                            "name": gcfg["name"],
                            "shed_kw": round(kw, 2),
                            "shed_priority": gcfg["shed_priority"],
                        }
                    if unshed_gap > 0.01:
                        shed_breakdown.append(f"无法继续切除缺口{unshed_gap:.2f}kW")

                    self.state.add_alert(
                        "LOAD_SHEDDING",
                        f"负荷缺口 {remaining_load:.2f}kW，按优先级甩负荷: {', '.join(shed_breakdown)}",
                        {"load_kw": load_kw, "total_available_kw": total_available,
                         "shed_kw": load_shed_kw, "shed_breakdown": shed_result,
                         "unshed_gap_kw": round(unshed_gap, 2)}
                    )
                    notes.append(f"警告：按优先级甩负荷 {load_shed_kw:.2f}kW ({', '.join(shed_breakdown)})")
                    audit_builder.add_branch(
                        "甩负荷决策",
                        True,
                        f"供电能力不足，按优先级甩负荷{load_shed_kw:.2f}kW: {', '.join(shed_breakdown)}",
                        {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                         "load_kw": load_kw, "shed_by_group": shed_result,
                         "unshed_gap_kw": round(unshed_gap, 2)}
                    )
            else:
                if prev_active_shed_count > 0:
                    surplus_for_restore = -remaining_load
                    if surplus_for_restore > 0:
                        restored, leftover = self.state.restore_load_groups_dynamic(
                            surplus_for_restore, now, dispatch_id
                        )
                        if restored:
                            restore_breakdown = []
                            for gid, kw in restored.items():
                                gcfg = config.LOAD_GROUP_CONFIG[gid]
                                restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
                                group_restore_details[gid] = {
                                    "group_id": gid,
                                    "name": gcfg["name"],
                                    "restored_kw": round(kw, 2),
                                    "restore_priority": gcfg["restore_priority"],
                                }
                            notes.append(f"[供电恢复] {', '.join(restore_breakdown)}")
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
                aggressiveness = self.strategy_params.battery_discharge_aggressiveness

                discharge_kw = min(remaining_load * aggressiveness, max_discharge)
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
                    tol_price = self.strategy_params.purchase_tolerance_price
                    if tol_price is not None and grid_buy_price < tol_price:
                        use_grid = True
                    else:
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
                    shed_threshold = self.strategy_params.shed_trigger_threshold_ratio
                    gap_ratio = remaining_load / load_kw if load_kw > 0 else 1.0
                    if gap_ratio <= shed_threshold and shed_threshold > 0:
                        grid_fill = remaining_load
                        grid_fill_cost = grid_fill * time_interval_hours * grid_buy_price
                        total_cost += grid_fill_cost
                        grid_import_kw += grid_fill
                        load_grid_import_kwh += grid_fill * time_interval_hours
                        remaining_load = 0
                        notes.append(f"[甩负荷容忍带] 缺口占比{gap_ratio*100:.1f}% ≤ 阈值{shed_threshold*100:.0f}%，强制购电填补{grid_fill:.2f}kW (成本{grid_fill_cost:.4f}元)")
                        audit_builder.add_branch(
                            "甩负荷决策",
                            False,
                            f"缺口占比{gap_ratio*100:.1f}%低于阈值{shed_threshold*100:.0f}%，容忍带内强制购电填补",
                            {"gap_ratio": gap_ratio, "threshold": shed_threshold, "grid_fill_kw": grid_fill, "reason": "shed_tolerance_band"}
                        )
                    else:
                        total_available = total_renewable + bess_action[bes_id]["discharge_kw"] + diesel_output[ds_id] + grid_import_kw
                        shed_result, unshed_gap = self.state.compute_priority_load_shedding_dynamic(
                            remaining_load, now, dispatch_id
                        )
                        total_shed_actual = sum(shed_result.values())
                        load_shed_kw = total_shed_actual + unshed_gap

                        shed_breakdown = []
                        for gid, kw in shed_result.items():
                            gcfg = config.LOAD_GROUP_CONFIG[gid]
                            shed_breakdown.append(f"{gcfg['name']}切{kw:.2f}kW")
                            if gid not in group_shed_details:
                                group_shed_details[gid] = {
                                    "group_id": gid,
                                    "name": gcfg["name"],
                                    "shed_kw": round(kw, 2),
                                    "shed_priority": gcfg["shed_priority"],
                                }
                            else:
                                group_shed_details[gid]["shed_kw"] = round(group_shed_details[gid]["shed_kw"] + kw, 2)
                        if unshed_gap > 0.01:
                            shed_breakdown.append(f"无法继续切除缺口{unshed_gap:.2f}kW")

                        self.state.add_alert(
                            "LOAD_SHEDDING",
                            f"负荷缺口 {remaining_load:.2f}kW，按优先级甩负荷: {', '.join(shed_breakdown)}",
                            {"load_kw": load_kw, "total_available_kw": total_available,
                             "shed_kw": load_shed_kw, "shed_breakdown": shed_result,
                             "unshed_gap_kw": round(unshed_gap, 2)}
                        )
                        notes.append(f"警告：按优先级甩负荷 {load_shed_kw:.2f}kW ({', '.join(shed_breakdown)})")
                        audit_builder.add_branch(
                            "甩负荷决策",
                            True,
                            f"供电能力不足，按优先级甩负荷{load_shed_kw:.2f}kW: {', '.join(shed_breakdown)}",
                            {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                             "load_kw": load_kw, "shed_by_group": shed_result,
                             "unshed_gap_kw": round(unshed_gap, 2)}
                        )
                else:
                    if prev_active_shed_count > 0 and remaining_load < 0:
                        surplus_for_restore = -remaining_load
                        if surplus_for_restore > 0:
                            restored, leftover = self.state.restore_load_groups_dynamic(
                                surplus_for_restore, now, dispatch_id
                            )
                            if restored:
                                restore_breakdown = []
                                for gid, kw in restored.items():
                                    gcfg = config.LOAD_GROUP_CONFIG[gid]
                                    restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
                                    if gid not in group_restore_details:
                                        group_restore_details[gid] = {
                                            "group_id": gid,
                                            "name": gcfg["name"],
                                            "restored_kw": round(kw, 2),
                                            "restore_priority": gcfg["restore_priority"],
                                        }
                                    else:
                                        group_restore_details[gid]["restored_kw"] = round(group_restore_details[gid]["restored_kw"] + kw, 2)
                                notes.append(f"[供电恢复] {', '.join(restore_breakdown)}")
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

                total_surplus_for_restore = (-remaining_load)
                if prev_active_shed_count > 0 and total_surplus_for_restore > 0:
                    restored, leftover = self.state.restore_load_groups_dynamic(
                        total_surplus_for_restore, now, dispatch_id
                    )
                    if restored:
                        restore_breakdown = []
                        for gid, kw in restored.items():
                            gcfg = config.LOAD_GROUP_CONFIG[gid]
                            restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
                            if gid not in group_restore_details:
                                group_restore_details[gid] = {
                                    "group_id": gid,
                                    "name": gcfg["name"],
                                    "restored_kw": round(kw, 2),
                                    "restore_priority": gcfg["restore_priority"],
                                }
                            else:
                                group_restore_details[gid]["restored_kw"] = round(group_restore_details[gid]["restored_kw"] + kw, 2)
                        notes.append(f"[供电恢复] {', '.join(restore_breakdown)}")

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
                aggressiveness = self.strategy_params.battery_discharge_aggressiveness
                discharge_kw = min(remaining_load * aggressiveness, max_discharge)
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
                    tol_price = self.strategy_params.purchase_tolerance_price
                    if tol_price is not None and grid_buy_price < tol_price:
                        use_grid = True
                    else:
                        use_grid = grid_buy_price < diesel_gen_cost
                    diesel_cap = self.state.get_available_diesel_capacity(ds_id, now)

                    carbon_warning = False
                    carbon_emergency = False
                    if carbon_enabled and carbon_status_info:
                        if carbon_status_info["status"] == "warning":
                            carbon_warning = True
                        elif carbon_status_info["status"] in ("emergency", "exceeded"):
                            carbon_emergency = True

                    if carbon_emergency:
                        grid_limit_ratio = config.CARBON_CONFIG["emergency_grid_limit_ratio"]
                        grid_import_load = remaining_load * grid_limit_ratio
                        remaining_after_grid = remaining_load - grid_import_load
                        carbon_shed_occurred = True

                        load_cost = grid_import_load * time_interval_hours * grid_buy_price
                        total_cost += load_cost
                        grid_import_kw += grid_import_load
                        load_grid_import_kwh += grid_import_load * time_interval_hours
                        remaining_load = remaining_after_grid

                        notes.append(
                            f"[碳紧急] 购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                            f"购电{grid_import_load:.2f}kW，剩余{remaining_after_grid:.2f}kW走甩负荷"
                        )
                        audit_builder.add_branch(
                            "购电决策",
                            True,
                            f"碳紧急状态，购电限制在缺口的{grid_limit_ratio*100:.0f}%，"
                            f"购电{grid_import_load:.2f}kW",
                            {"grid_import_kw": grid_import_load, "reason": "carbon_emergency_limit",
                             "limit_ratio": grid_limit_ratio}
                        )
                        audit_builder.add_branch(
                            "柴油机启动决策",
                            False,
                            "碳紧急状态，禁止启动柴油机",
                            {"reason": "carbon_emergency_prohibition"}
                        )
                    elif carbon_warning:
                        grid_import_load = remaining_load
                        remaining_after_grid = 0
                        load_cost = grid_import_load * time_interval_hours * grid_buy_price

                        if diesel_cap["can_run"] and not diesel_cap.get("startup_cost_applies"):
                            diesel_kw = min(remaining_load, diesel_cap["max_output"])
                            if diesel_kw * time_interval_hours * diesel_gen_cost < load_cost:
                                diesel_output[ds_id] = diesel_kw
                                remaining_after_grid = remaining_load - diesel_kw
                                self.state.diesel_state[ds_id].output_kw = diesel_kw
                                total_cost += diesel_kw * time_interval_hours * diesel_gen_cost
                                grid_import_load = 0
                                notes.append(f"柴油机持续运行，出力 {diesel_kw:.2f}kW (碳预警：已运行柴油机可继续使用)")
                                audit_builder.add_branch(
                                    "购电决策",
                                    False,
                                    "碳预警状态，柴油机已运行且更经济，不购电",
                                    {"reason": "carbon_warning_diesel_running"}
                                )
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    True,
                                    "碳预警状态，柴油机已运行，可持续出力",
                                    {"diesel_output_kw": diesel_kw, "already_running": True,
                                     "reason": "carbon_warning_diesel_running"}
                                )
                            else:
                                total_cost += load_cost
                                grid_import_kw += grid_import_load
                                load_grid_import_kwh += grid_import_load * time_interval_hours
                                remaining_after_grid = 0
                                notes.append(f"[碳预警] 外购电 {grid_import_load:.2f}kW (禁止经济性启动柴油机)")
                                audit_builder.add_branch(
                                    "购电决策",
                                    True,
                                    "碳预警状态，禁止经济性启动柴油机，选择购电",
                                    {"grid_import_kw": grid_import_load, "reason": "carbon_warning_no_diesel_startup"}
                                )
                                audit_builder.add_branch(
                                    "柴油机启动决策",
                                    False,
                                    "碳预警状态，禁止经济性启动柴油机",
                                    {"reason": "carbon_warning_prohibition"}
                                )
                        else:
                            total_cost += load_cost
                            grid_import_kw += grid_import_load
                            load_grid_import_kwh += grid_import_load * time_interval_hours
                            remaining_after_grid = 0
                            notes.append(f"[碳预警] 外购电 {grid_import_load:.2f}kW (禁止经济性启动柴油机)")
                            audit_builder.add_branch(
                                "购电决策",
                                True,
                                "碳预警状态，禁止经济性启动柴油机，选择购电",
                                {"grid_import_kw": grid_import_load, "reason": "carbon_warning_no_diesel_startup"}
                            )
                            audit_builder.add_branch(
                                "柴油机启动决策",
                                False,
                                "碳预警状态，禁止经济性启动柴油机",
                                {"reason": "carbon_warning_prohibition"}
                            )
                        remaining_load = remaining_after_grid
                    else:
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
                                    diesel_startup_occurred = True
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
                        shed_threshold = self.strategy_params.shed_trigger_threshold_ratio
                        gap_ratio = remaining_load / load_kw if load_kw > 0 else 1.0
                        if gap_ratio <= shed_threshold and shed_threshold > 0:
                            grid_fill = remaining_load
                            grid_fill_cost = grid_fill * time_interval_hours * grid_buy_price
                            total_cost += grid_fill_cost
                            grid_import_kw += grid_fill
                            load_grid_import_kwh += grid_fill * time_interval_hours
                            remaining_load = 0
                            notes.append(f"[甩负荷容忍带] 缺口占比{gap_ratio*100:.1f}% ≤ 阈值{shed_threshold*100:.0f}%，强制购电填补{grid_fill:.2f}kW (成本{grid_fill_cost:.4f}元)")
                            audit_builder.add_branch(
                                "甩负荷决策",
                                False,
                                f"缺口占比{gap_ratio*100:.1f}%低于阈值{shed_threshold*100:.0f}%，容忍带内强制购电填补",
                                {"gap_ratio": gap_ratio, "threshold": shed_threshold, "grid_fill_kw": grid_fill, "reason": "shed_tolerance_band"}
                            )
                        else:
                            total_available = total_renewable + bess_action[bes_id]["discharge_kw"] + diesel_output[ds_id] + grid_import_kw
                            shed_result, unshed_gap = self.state.compute_priority_load_shedding_dynamic(
                                remaining_load, now, dispatch_id
                            )
                            total_shed_actual = sum(shed_result.values())
                            load_shed_kw = total_shed_actual + unshed_gap

                            shed_breakdown = []
                            for gid, kw in shed_result.items():
                                gcfg = config.LOAD_GROUP_CONFIG[gid]
                                shed_breakdown.append(f"{gcfg['name']}切{kw:.2f}kW")
                                if gid not in group_shed_details:
                                    group_shed_details[gid] = {
                                        "group_id": gid,
                                        "name": gcfg["name"],
                                        "shed_kw": round(kw, 2),
                                        "shed_priority": gcfg["shed_priority"],
                                    }
                                else:
                                    group_shed_details[gid]["shed_kw"] = round(group_shed_details[gid]["shed_kw"] + kw, 2)
                            if unshed_gap > 0.01:
                                shed_breakdown.append(f"无法继续切除缺口{unshed_gap:.2f}kW")

                            self.state.add_alert(
                                "LOAD_SHEDDING",
                                f"负荷缺口 {remaining_load:.2f}kW，按优先级甩负荷: {', '.join(shed_breakdown)}",
                                {"load_kw": load_kw, "total_available_kw": total_available,
                                 "shed_kw": load_shed_kw, "shed_breakdown": shed_result,
                                 "unshed_gap_kw": round(unshed_gap, 2)}
                            )
                            notes.append(f"警告：按优先级甩负荷 {load_shed_kw:.2f}kW ({', '.join(shed_breakdown)})")
                            audit_builder.add_branch(
                                "甩负荷决策",
                                True,
                                f"所有可用源仍无法覆盖负荷，按优先级甩负荷{load_shed_kw:.2f}kW: {', '.join(shed_breakdown)}",
                                {"shed_kw": load_shed_kw, "total_available_kw": total_available,
                                 "load_kw": load_kw, "shed_by_group": shed_result,
                                 "unshed_gap_kw": round(unshed_gap, 2)}
                            )
                    else:
                        if prev_active_shed_count > 0 and remaining_load < 0:
                            surplus_for_restore = -remaining_load
                            if surplus_for_restore > 0:
                                restored, leftover = self.state.restore_load_groups_dynamic(
                                    surplus_for_restore, now, dispatch_id
                                )
                                if restored:
                                    restore_breakdown = []
                                    for gid, kw in restored.items():
                                        gcfg = config.LOAD_GROUP_CONFIG[gid]
                                        restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
                                        if gid not in group_restore_details:
                                            group_restore_details[gid] = {
                                                "group_id": gid,
                                                "name": gcfg["name"],
                                                "restored_kw": round(kw, 2),
                                                "restore_priority": gcfg["restore_priority"],
                                            }
                                        else:
                                            group_restore_details[gid]["restored_kw"] = round(group_restore_details[gid]["restored_kw"] + kw, 2)
                                    notes.append(f"[供电恢复] {', '.join(restore_breakdown)}")
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

                total_surplus_for_restore = (-remaining_load)
                if prev_active_shed_count > 0 and total_surplus_for_restore > 0:
                    restored, leftover = self.state.restore_load_groups_dynamic(
                        total_surplus_for_restore, now, dispatch_id
                    )
                    if restored:
                        restore_breakdown = []
                        for gid, kw in restored.items():
                            gcfg = config.LOAD_GROUP_CONFIG[gid]
                            restore_breakdown.append(f"{gcfg['name']}恢复{kw:.2f}kW")
                            if gid not in group_restore_details:
                                group_restore_details[gid] = {
                                    "group_id": gid,
                                    "name": gcfg["name"],
                                    "restored_kw": round(kw, 2),
                                    "restore_priority": gcfg["restore_priority"],
                                }
                            else:
                                group_restore_details[gid]["restored_kw"] = round(group_restore_details[gid]["restored_kw"] + kw, 2)
                        notes.append(f"[供电恢复] {', '.join(restore_breakdown)}")

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

        for bid in bess_action:
            if bid in bess_maintenance_disabled:
                if bess_action[bid]["charge_kw"] > 0 or bess_action[bid]["discharge_kw"] > 0:
                    notes.append(f"[维保停用] 储能电池{bid}维保中，强制充放电功率为0")
                bess_action[bid]["charge_kw"] = 0.0
                bess_action[bid]["discharge_kw"] = 0.0

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

        extra_forced_shed = 0.0
        if config.DYNAMIC_SHED_CONFIG.get("enable_dynamic_shed", False):
            bes_id = list(config.BESS_CONFIG.keys())[0]
            bs = self.state.bess_state.get(bes_id)
            soc = bs.soc if bs else 1.0
            mode = self.state.current_shed_mode
            if mode == "emergency" and soc < config.DYNAMIC_SHED_CONFIG["emergency_forced_shed_soc_threshold"]:
                forced_gap = 0.0
                shed_result, _ = self.state.compute_priority_load_shedding_dynamic(
                    forced_gap, now, dispatch_id
                )
                if shed_result:
                    forced_shed_total = sum(shed_result.values())
                    extra_forced_shed = forced_shed_total
                    load_shed_kw += forced_shed_total
                    shed_breakdown = []
                    for gid, kw in shed_result.items():
                        gcfg = config.LOAD_GROUP_CONFIG[gid]
                        shed_breakdown.append(f"{gcfg['name']}切{kw:.2f}kW")
                        if gid in group_shed_details:
                            group_shed_details[gid]["shed_kw"] = round(
                                group_shed_details[gid]["shed_kw"] + kw, 2
                            )
                        else:
                            group_shed_details[gid] = {
                                "group_id": gid,
                                "name": gcfg["name"],
                                "shed_kw": round(kw, 2),
                                "shed_priority": gcfg["shed_priority"],
                            }
                    notes.append(
                        f"[紧急保护] SOC低于{config.DYNAMIC_SHED_CONFIG['emergency_forced_shed_soc_threshold']*100:.0f}%，"
                        f"强制切除三级群组: {', '.join(shed_breakdown)} (合计{forced_shed_total:.2f}kW)"
                    )
                    self.state.add_alert(
                        "EMERGENCY_FORCED_SHED",
                        f"紧急模式SOC保护，强制切除负荷: {', '.join(shed_breakdown)}",
                        {"soc": soc, "shed_kw": forced_shed_total,
                         "shed_by_group": shed_result, "threshold": config.DYNAMIC_SHED_CONFIG["emergency_forced_shed_soc_threshold"]}
                    )
                    audit_builder.add_branch(
                        "紧急保护强制切负荷",
                        True,
                        f"SOC({soc*100:.1f}%)低于阈值({config.DYNAMIC_SHED_CONFIG['emergency_forced_shed_soc_threshold']*100:.0f}%)，"
                        f"强制切除三级群组负荷{forced_shed_total:.2f}kW",
                        {"soc": soc, "threshold": config.DYNAMIC_SHED_CONFIG["emergency_forced_shed_soc_threshold"],
                         "shed_kw": forced_shed_total, "shed_by_group": shed_result}
                    )

        self.state.stats.total_load_shed_kwh += (load_shed_kw) * time_interval_hours

        self.state.finalize_group_state_after_dispatch()
        self.state.record_reliability_snapshot(now)

        if island_grid_blocked:
            if grid_import_kw > 0.001:
                island_remaining = grid_import_kw
                grid_import_kw = 0.0
                ds_id_island = list(config.DIESEL_CONFIG.keys())[0]
                diesel_cap = self.state.get_available_diesel_capacity(ds_id_island, now)
                if diesel_cap["can_run"]:
                    diesel_available_kw = diesel_cap["max_output"] - diesel_output.get(ds_id_island, 0.0)
                    diesel_fill = min(island_remaining, diesel_available_kw)
                    if diesel_fill > 0:
                        diesel_output[ds_id_island] = diesel_output.get(ds_id_island, 0.0) + diesel_fill
                        self.state.diesel_state[ds_id_island].output_kw = diesel_output[ds_id_island]
                        total_cost += diesel_fill * time_interval_hours * diesel_gen_cost
                        if diesel_cap.get("startup_cost_applies"):
                            startup_cost = config.DIESEL_CONFIG[ds_id_island]["startup_cost"]
                            total_cost += startup_cost
                            self.state.start_diesel(ds_id_island, now)
                            notes.append(f"[孤岛] 启动柴油机填补购电缺口，出力 {diesel_fill:.2f}kW")
                        else:
                            notes.append(f"[孤岛] 柴油机增出力填补购电缺口 {diesel_fill:.2f}kW")
                        island_remaining -= diesel_fill

                bes_id_island = list(config.BESS_CONFIG.keys())[0]
                if island_remaining > 0.01:
                    max_discharge = self.state.get_bess_max_discharge(bes_id_island, time_interval_hours)
                    island_soc_min = self.state.island_manager.get_island_soc_min()
                    cfg_bess = config.BESS_CONFIG[bes_id_island]
                    bs = self.state.bess_state[bes_id_island]
                    if bs.soc > island_soc_min:
                        energy_avail = (bs.soc - island_soc_min) * cfg_bess["capacity_kwh"]
                        max_by_island_soc = energy_avail * cfg_bess["discharge_efficiency"] / time_interval_hours
                        island_max_discharge = min(max_discharge, max_by_island_soc,
                                                   cfg_bess["max_discharge_power"] - bess_action[bes_id_island].get("discharge_kw", 0.0))
                        batt_fill = min(island_remaining, island_max_discharge)
                        if batt_fill > 0:
                            bess_action[bes_id_island]["discharge_kw"] = bess_action[bes_id_island].get("discharge_kw", 0.0) + batt_fill
                            island_remaining -= batt_fill
                            notes.append(f"[孤岛] 电池放电填补购电缺口 {batt_fill:.2f}kW")

                if island_remaining > 0.01:
                    shed_result_island, unshed_gap = self.state.compute_priority_load_shedding_dynamic(
                        island_remaining, now, dispatch_id
                    )
                    total_shed_actual = sum(shed_result_island.values())
                    load_shed_kw += total_shed_actual + unshed_gap
                    if total_shed_actual > 0.01:
                        shed_breakdown_island = []
                        for gid, kw in shed_result_island.items():
                            gcfg = config.LOAD_GROUP_CONFIG[gid]
                            shed_breakdown_island.append(f"{gcfg['name']}切{kw:.2f}kW")
                            group_shed_details[gid] = {
                                "group_id": gid,
                                "name": gcfg["name"],
                                "shed_kw": round(kw, 2),
                                "shed_priority": gcfg["shed_priority"],
                            }
                        notes.append(f"[孤岛] 供电不足，按优先级甩负荷: {', '.join(shed_breakdown_island)}")
                        self.state.add_alert(
                            "ISLAND_LOAD_SHEDDING",
                            f"孤岛模式供电不足，甩负荷 {total_shed_actual:.2f}kW",
                            {"shed_kw": total_shed_actual, "shed_by_group": shed_result_island}
                        )

            if grid_export_kw > 0.001:
                surplus_island = grid_export_kw
                grid_export_kw = 0.0
                total_cost += surplus_island * time_interval_hours * feed_in_price
                bes_id_surplus = list(config.BESS_CONFIG.keys())[0]
                cfg_bess_s = config.BESS_CONFIG[bes_id_surplus]
                bs_s = self.state.bess_state[bes_id_surplus]
                if bs_s.soc < cfg_bess_s["soc_max"]:
                    charge_space = (cfg_bess_s["soc_max"] - bs_s.soc) * cfg_bess_s["capacity_kwh"]
                    max_charge_by_space = charge_space / cfg_bess_s["charge_efficiency"] / time_interval_hours
                    charge_kw = min(surplus_island, cfg_bess_s["max_charge_power"], max_charge_by_space)
                    if charge_kw > 0:
                        bess_action[bes_id_surplus]["charge_kw"] = bess_action[bes_id_surplus].get("charge_kw", 0.0) + charge_kw
                        surplus_island -= charge_kw
                        notes.append(f"[孤岛] 盈余充电 {charge_kw:.2f}kW (无法上网)")
                notes.append(f"[孤岛] 弃电 {surplus_island:.2f}kW (无法上网售电)")

            self.state.stats.total_grid_import_kwh = max(0, self.state.stats.total_grid_import_kwh - grid_import_kw * time_interval_hours)
            self.state.stats.total_grid_export_kwh = max(0, self.state.stats.total_grid_export_kwh)

        island_mgr = self.state.island_manager
        total_renewable_for_update = sum(pv_output.values()) + sum(wt_output.values())
        diesel_kw_for_update = sum(diesel_output.values())
        diesel_kwh_for_update = sum(diesel_output.values()) * time_interval_hours
        island_mgr.update_dispatch_cycle(
            now=now,
            total_renewable_kw=total_renewable_for_update,
            diesel_kw=diesel_kw_for_update,
            load_shed_kw=load_shed_kw,
            diesel_generated_kwh=diesel_kwh_for_update,
        )

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
            group_shed_details=group_shed_details,
            group_restore_details=group_restore_details,
        )

        if self.dr_manager:
            self.dr_manager.start_event_if_due(now)
            self.dr_manager.check_and_finish_events(now)
            decision = self.dr_manager.apply_dr_constraints(decision, now)

        self.state.add_dispatch(decision)

        reactive_compensation_result = None
        if config.REACTIVE_POWER_CONFIG.get("enable_reactive_compensation", False):
            total_active_kw = load_served
            total_reactive_kvar = self.state.get_total_reactive_load_kvar()
            reactive_compensation_result = self.state.reactive_power_manager.execute_compensation(
                active_kw=total_active_kw,
                reactive_kvar=total_reactive_kvar,
                dispatch_id=dispatch_id,
                now=now,
            )
            if reactive_compensation_result.get("action_taken"):
                switch_event = reactive_compensation_result.get("switch_event")
                if switch_event:
                    notes.append(
                        f"[无功补偿] 电容组投切: {switch_event.groups_before}组 → {switch_event.groups_after}组, "
                        f"功率因数: {switch_event.pf_before:.4f} → {switch_event.pf_after:.4f}, "
                        f"原因: {switch_event.reason}"
                    )
            assessment = reactive_compensation_result.get("assessment")
            if assessment and not assessment.is_compliant:
                notes.append(
                    f"[功率因数考核] 不达标: 当前{assessment.power_factor:.4f}, "
                    f"目标{assessment.target_power_factor}, 罚款{assessment.penalty_amount:.2f}元"
                )
                self.state.add_alert(
                    "POWER_FACTOR_PENALTY",
                    f"功率因数不达标，本次调度罚款{assessment.penalty_amount:.2f}元，"
                    f"月度累计罚款{self.state.reactive_power_manager.reactive_state.monthly_penalty:.2f}元",
                    {
                        "dispatch_id": dispatch_id,
                        "power_factor": assessment.power_factor,
                        "target_power_factor": assessment.target_power_factor,
                        "penalty_amount": assessment.penalty_amount,
                        "monthly_penalty": self.state.reactive_power_manager.reactive_state.monthly_penalty,
                    }
                )
            if reactive_compensation_result.get("limited_event"):
                limited_event = reactive_compensation_result.get("limited_event")
                notes.append(
                    f"[补偿受限] {limited_event.reason}"
                )
                self.state.add_alert(
                    "REACTIVE_COMPENSATION_LIMITED",
                    f"无功补偿受限: {limited_event.reason}",
                    {
                        "dispatch_id": dispatch_id,
                        "current_power_factor": limited_event.current_power_factor,
                        "required_groups": limited_event.required_capacitor_groups,
                        "available_groups": limited_event.available_capacitor_groups,
                        "constraint_type": limited_event.constraint_type,
                    }
                )

        carbon_record = None
        carbon_exceed_penalty = 0.0
        if carbon_enabled:
            total_diesel_kwh = sum(diesel_output.values()) * time_interval_hours
            total_grid_kwh = grid_import_kw * time_interval_hours
            carbon_record = self.state.carbon_manager.record_emission(
                dispatch_id, total_diesel_kwh, total_grid_kwh, now
            )
            if carbon_record:
                carbon_exceed_penalty = self.state.carbon_manager.calculate_exceed_penalty(
                    carbon_record.total_emission_kg, grid_buy_price
                )
                if carbon_exceed_penalty > 0:
                    total_cost += carbon_exceed_penalty
                    decision.cost = total_cost
                    notes.append(f"[碳超标] 碳排放超标罚款 {carbon_exceed_penalty:.4f}元")
                    self.state.add_alert(
                        "CARBON_EXCEED_PENALTY",
                        f"碳排放超标，本次调度罚款 {carbon_exceed_penalty:.4f}元，"
                        f"累计排放 {self.state.carbon_manager.quota_state.accumulated_emission_kg:.2f}kgCO2",
                        {
                            "dispatch_id": dispatch_id,
                            "penalty_amount": carbon_exceed_penalty,
                            "total_emission_kg": carbon_record.total_emission_kg,
                            "accumulated_emission_kg": self.state.carbon_manager.quota_state.accumulated_emission_kg,
                            "monthly_quota_kg": self.state.carbon_manager.quota_state.monthly_quota_kg,
                        }
                    )

        if carbon_shed_occurred:
            from models import AnomalyMarker
            carbon_anomaly = AnomalyMarker(
                anomaly_type="CARBON_CONSTRAINT_SHED",
                severity="high",
                description="碳配额约束导致甩负荷，非供电能力不足",
                details={
                    "carbon_status": carbon_status_info["status"] if carbon_status_info else "unknown",
                    "shed_kw": load_shed_kw,
                    "reason": "碳配额约束限制购电和柴油机使用",
                }
            )

        audit_id = self.state.generate_audit_id()
        audit_log = audit_builder.build(decision, audit_id)

        anomalies = self.anomaly_detector.detect_all(audit_log)
        if carbon_shed_occurred:
            anomalies.append(carbon_anomaly)
        audit_log.anomalies = anomalies

        self.state.add_audit_log(audit_log)

        cost_analyzer = CostAttributionAnalyzer(
            state=self.state,
            decision=decision,
            dispatch_id=dispatch_id,
            now=now,
            time_interval_hours=time_interval_hours,
            diesel_startup_occurred=diesel_startup_occurred,
            carbon_exceed_penalty=carbon_exceed_penalty
        )
        cost_attribution = cost_analyzer.compute_attribution()
        self.state.add_cost_attribution(cost_attribution)

        missed_opportunity = cost_analyzer.compute_missed_opportunity()
        self.state.add_missed_opportunity(missed_opportunity)

        if dual_enabled and shadow_state_copy is not None:
            try:
                main_cost = decision.cost
                main_shed_kw = decision.load_shed_kw
                main_bess_discharge = 0.0
                for bes_id, act in decision.bess_action.items():
                    main_bess_discharge += act.get("discharge_kw", 0.0)

                shadow_engine = DispatchEngine(
                    state=shadow_state_copy,
                    dr_manager=None,
                    price_forecast_manager=None,
                    strategy_params=ds_manager.shadow_strategy,
                )
                shadow_decision = shadow_engine.execute(now=now)

                shadow_cost = shadow_decision.cost
                shadow_shed_kw = shadow_decision.load_shed_kw
                shadow_bess_discharge = 0.0
                for bes_id, act in shadow_decision.bess_action.items():
                    shadow_bess_discharge += act.get("discharge_kw", 0.0)

                ds_manager.record_round(
                    main_cost=main_cost,
                    main_shed_kw=main_shed_kw,
                    shadow_cost=shadow_cost,
                    shadow_shed_kw=shadow_shed_kw,
                    time_interval_hours=time_interval_hours,
                    main_battery_discharge_kw=main_bess_discharge,
                    shadow_battery_discharge_kw=shadow_bess_discharge,
                    now=now,
                )

                if ds_manager.should_evaluate():
                    eval_result = ds_manager.evaluate_and_maybe_switch(now=now, trigger="auto_cycle")
                    if eval_result.get("switched"):
                        decision.notes.append(
                            f"[策略自动切换] {eval_result.get('note', '')} "
                            f"(评估{eval_result.get('rounds_evaluated', 0)}轮，"
                            f"成本改善{eval_result.get('cost_improvement_ratio', 0)*100:.1f}%)"
                        )
                    else:
                        decision.notes.append(
                            f"[策略周期评估] {eval_result.get('note', '')}"
                        )
            except Exception as e:
                decision.notes.append(f"[双策略异常] 影子策略执行或评估失败: {str(e)}")

        if self.state.arbitrage_analyzer:
            bes_id = list(config.BESS_CONFIG.keys())[0]
            bess_act = decision.bess_action.get(bes_id, {})
            soc_before_val = bess_act.get("soc_before", 0.0)
            soc_after_val = bess_act.get("soc_after", 0.0)

            grid_charge_kw = 0.0
            renewable_charge_kw = 0.0
            charge_total = bess_act.get("charge_kw", 0.0)
            if charge_total > 0:
                total_renewable = self.state.get_total_renewable_kw()
                renewable_for_load = min(total_renewable, load_kw)
                renewable_surplus = max(0.0, total_renewable - renewable_for_load)
                renewable_charge_kw = min(renewable_surplus, charge_total)
                grid_charge_kw = max(0.0, charge_total - renewable_charge_kw)

            plan_active_val = True
            plan_abnormal_val = False
            if current_hour_plan:
                plan_active_val = current_hour_plan.active
                plan_abnormal_val = current_hour_plan.abnormal

            try:
                self.state.arbitrage_analyzer.record_dispatch(
                    decision=decision,
                    now=now,
                    time_interval_hours=time_interval_hours,
                    storage_mode=storage_mode,
                    plan_active=plan_active_val,
                    plan_abnormal=plan_abnormal_val,
                    soc_before=soc_before_val,
                    soc_after=soc_after_val,
                    charge_from_grid_kw=grid_charge_kw,
                    charge_from_renewable_kw=renewable_charge_kw,
                )
            except Exception:
                pass

            try:
                self.state.arbitrage_analyzer.auto_settle_if_due(now)
            except Exception:
                pass

        return decision
