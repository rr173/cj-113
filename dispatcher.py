from datetime import datetime
from typing import Dict, Any, Tuple
import config
from models import MicrogridState, DispatchDecision


class DispatchEngine:
    def __init__(self, state: MicrogridState):
        self.state = state

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

        bess_action = {}
        for bes_id in config.BESS_CONFIG:
            bess_action[bes_id] = {"charge_kw": 0.0, "discharge_kw": 0.0, "soc_before": self.state.bess_state[bes_id].soc}

        grid_import_kw = 0.0
        grid_export_kw = 0.0
        load_shed_kw = 0.0
        notes = []
        total_cost = 0.0

        tariff_period = config.get_tariff_period(now.hour)
        grid_buy_price = config.GRID_TARIFF[tariff_period]["price"]
        diesel_gen_cost = config.DIESEL_CONFIG[list(config.DIESEL_CONFIG.keys())[0]]["generation_cost"]
        feed_in_price = config.FEED_IN_TARIFF

        remaining_load = load_kw - total_renewable

        bes_id = list(config.BESS_CONFIG.keys())[0]
        ds_id = list(config.DIESEL_CONFIG.keys())[0]

        if remaining_load > 0:
            max_discharge_base = self.state.get_bess_max_discharge(bes_id, time_interval_hours)
            max_discharge = self.state.get_bess_max_discharge_with_health(bes_id, time_interval_hours)
            discharge_kw = min(remaining_load, max_discharge)
            if discharge_kw > 0:
                bess_action[bes_id]["discharge_kw"] = discharge_kw
                remaining_load -= discharge_kw
                notes.append(f"电池放电 {discharge_kw:.2f}kW")

                bh = self.state.bess_state[bes_id].health
                cfg = config.BESS_CONFIG[bes_id]
                if bh.health_percent < cfg["health_derating_threshold"]:
                    notes.append(f"电池健康度{bh.health_percent:.1f}% < {cfg['health_derating_threshold']}%，已降额{cfg['power_derating_ratio']*100:.0f}%运行")

            if remaining_load > 0:
                use_grid = grid_buy_price < diesel_gen_cost
                diesel_cap = self.state.get_available_diesel_capacity(ds_id, now)

                if use_grid:
                    grid_import_kw = remaining_load
                    remaining_load = 0
                    total_cost += grid_import_kw * time_interval_hours * grid_buy_price
                    notes.append(f"外购电 {grid_import_kw:.2f}kW (电价 {grid_buy_price:.2f}元/kWh，低于柴油成本)")
                else:
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
                        else:
                            notes.append(f"柴油机恢复/持续运行，出力 {diesel_kw:.2f}kW")
                    else:
                        notes.append(f"柴油机不可用: {diesel_cap.get('reason', '未知原因')}，且购电价({grid_buy_price})高于柴油成本({diesel_gen_cost})，不选择买电")

                if remaining_load > 0:
                    load_shed_kw = remaining_load
                    total_available = total_renewable + bess_action[bes_id]["discharge_kw"] + diesel_output[ds_id] + grid_import_kw
                    self.state.add_alert(
                        "LOAD_SHEDDING",
                        f"负荷缺口 {load_shed_kw:.2f}kW，执行甩负荷",
                        {"load_kw": load_kw, "total_available_kw": total_available, "shed_kw": load_shed_kw}
                    )
                    notes.append(f"警告：甩负荷 {load_shed_kw:.2f}kW (本地所有可用源出力不足)")

        elif remaining_load < 0:
            surplus = -remaining_load
            max_charge = self.state.get_bess_max_charge_with_health(bes_id, time_interval_hours)
            charge_kw = min(surplus, max_charge)
            if charge_kw > 0:
                bess_action[bes_id]["charge_kw"] = charge_kw
                surplus -= charge_kw
                notes.append(f"电池充电 {charge_kw:.2f}kW")

                bh = self.state.bess_state[bes_id].health
                cfg = config.BESS_CONFIG[bes_id]
                if bh.health_percent < cfg["health_derating_threshold"]:
                    notes.append(f"电池健康度{bh.health_percent:.1f}% < {cfg['health_derating_threshold']}%，已降额{cfg['power_derating_ratio']*100:.0f}%运行")

            if surplus > 0:
                grid_export_kw = surplus
                surplus = 0
                revenue = grid_export_kw * time_interval_hours * feed_in_price
                total_cost -= revenue
                notes.append(f"余电上网 {grid_export_kw:.2f}kW (收入 {revenue:.2f}元)")

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
            else:
                diesel_output[ds_id] = 0.0
                notes.append(f"柴油机空载维持运行 (已运行 {elapsed:.1f}分钟，需满 {diesel_cfg['min_runtime_minutes']}分钟才能停机)")
                self.state.diesel_state[ds_id].output_kw = 0.0

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

        for sid, kw in pv_output.items():
            self.state.stats.total_pv_generated_kwh[sid] += kw * time_interval_hours
        for sid, kw in wt_output.items():
            self.state.stats.total_wt_generated_kwh[sid] += kw * time_interval_hours

        diesel_kwh = diesel_output[ds_id] * time_interval_hours
        self.state.diesel_state[ds_id].total_generated_kwh += diesel_kwh
        self.state.stats.total_diesel_generated_kwh += diesel_kwh

        self.state.stats.total_grid_import_kwh += grid_import_kw * time_interval_hours
        self.state.stats.total_grid_export_kwh += grid_export_kw * time_interval_hours
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

        self.state.add_dispatch(decision)
        return decision
