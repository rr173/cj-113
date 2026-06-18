import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    MicrogridState,
    SimulationStatus,
)
from simulation import SimulationEngine
import config


def test_scenario_crud():
    print("=" * 60)
    print("测试 1: 场景 CRUD 操作")
    print("=" * 60)

    real_state = MicrogridState()
    sim_engine = SimulationEngine(real_state)

    scenario_data = {
        "name": "阴天测试场景",
        "description": "光伏出力仅为额定20%的测试场景",
        "duration_hours": 6,
        "time_step_minutes": 15,
        "pv_series": {
            "pv1": {
                "source_id": "pv1",
                "source_type": "pv",
                "segments": [
                    {"start_minute": 120, "end_minute": 240, "value_kw": 20.0},
                    {"start_minute": 240, "end_minute": 360, "value_kw": 30.0},
                ]
            },
            "pv2": {
                "source_id": "pv2",
                "source_type": "pv",
                "segments": [
                    {"start_minute": 120, "end_minute": 240, "value_kw": 20.0},
                    {"start_minute": 240, "end_minute": 360, "value_kw": 30.0},
                ]
            }
        },
        "load_series": {
            "segments": [
                {"start_minute": 0, "end_minute": 360, "value_kw": 250.0},
            ]
        },
        "diesel_available": {"ds1": True},
        "initial_soc_override": {"bes1": 0.6},
    }

    scenario = sim_engine.create_scenario(scenario_data)
    print(f"  [OK] 创建场景: {scenario.scenario_id} - {scenario.name}")
    print(f"       时长: {scenario.duration_hours}h, 步长: {scenario.time_step_minutes}min")
    print(f"       光伏时序分段数: pv1={len(scenario.pv_series['pv1'].segments)}, pv2={len(scenario.pv_series['pv2'].segments)}")

    scenarios = sim_engine.list_scenarios()
    assert len(scenarios) == 1, f"期望1个场景，实际{len(scenarios)}个"
    print(f"  [OK] 场景列表: {len(scenarios)} 个场景")

    fetched = sim_engine.get_scenario(scenario.scenario_id)
    assert fetched is not None
    assert fetched.name == "阴天测试场景"
    print(f"  [OK] 查询场景详情成功")

    updated = sim_engine.update_scenario(scenario.scenario_id, {"name": "阴天测试场景(已修改)"})
    assert updated.name == "阴天测试场景(已修改)"
    print(f"  [OK] 更新场景名称成功")

    copied = sim_engine.copy_scenario(scenario.scenario_id, "阴天场景副本")
    assert copied is not None
    assert copied.name == "阴天场景副本"
    print(f"  [OK] 复制场景成功: {copied.scenario_id}")

    scenarios = sim_engine.list_scenarios()
    assert len(scenarios) == 2
    print(f"  [OK] 复制后场景列表: {len(scenarios)} 个场景")

    deleted = sim_engine.delete_scenario(scenario.scenario_id)
    assert deleted is True
    print(f"  [OK] 删除场景成功")

    scenarios = sim_engine.list_scenarios()
    assert len(scenarios) == 1
    print(f"  [OK] 删除后场景列表: {len(scenarios)} 个场景")

    print()


def test_simulation_execution():
    print("=" * 60)
    print("测试 2: 仿真执行")
    print("=" * 60)

    real_state = MicrogridState()
    real_state.bess_state["bes1"].soc = 0.5
    sim_engine = SimulationEngine(real_state)

    scenario_data = {
        "name": "典型日仿真",
        "description": "6小时短周期仿真验证",
        "duration_hours": 4,
        "time_step_minutes": 30,
        "pv_series": {
            "pv1": {
                "segments": [
                    {"start_minute": 0, "end_minute": 60, "value_kw": 0.0},
                    {"start_minute": 60, "end_minute": 180, "value_kw": 80.0},
                    {"start_minute": 180, "end_minute": 240, "value_kw": 50.0},
                ]
            },
            "pv2": {
                "segments": [
                    {"start_minute": 0, "end_minute": 60, "value_kw": 0.0},
                    {"start_minute": 60, "end_minute": 180, "value_kw": 60.0},
                    {"start_minute": 180, "end_minute": 240, "value_kw": 40.0},
                ]
            }
        },
        "wt_series": {
            "wt1": {
                "segments": [
                    {"start_minute": 0, "end_minute": 240, "value_kw": 20.0},
                ]
            }
        },
        "load_series": {
            "segments": [
                {"start_minute": 0, "end_minute": 120, "value_kw": 200.0},
                {"start_minute": 120, "end_minute": 240, "value_kw": 350.0},
            ]
        },
        "diesel_available": {"ds1": True},
    }

    scenario = sim_engine.create_scenario(scenario_data)
    print(f"  [OK] 创建测试场景: {scenario.name}")
    print(f"       总步数: {scenario.duration_hours * 60 // scenario.time_step_minutes} 步")

    report = sim_engine.run_simulation(scenario.scenario_id)
    assert report is not None
    assert report.status == SimulationStatus.COMPLETED, f"仿真状态: {report.status}, 错误: {report.error_message}"
    print(f"  [OK] 仿真执行完成: {report.simulation_id}")
    print(f"       状态: {report.status}")
    print(f"       总步数: {report.total_steps}, 已完成: {report.completed_steps}")

    assert report.total_steps == report.completed_steps
    print(f"  [OK] 所有步骤完成")

    print()
    print("  === 仿真报告摘要 ===")
    print(f"    总成本: {report.total_cost:.4f} 元")
    print(f"    总购电量: {report.total_grid_import_kwh:.4f} kWh")
    print(f"    峰时段购电: {report.peak_grid_import_kwh:.4f} kWh")
    print(f"    峰购电占比: {report.peak_grid_import_kwh / report.total_grid_import_kwh * 100:.1f}%" if report.total_grid_import_kwh > 0 else "    峰购电占比: N/A")
    print(f"    总售电量: {report.total_grid_export_kwh:.4f} kWh")
    print(f"    柴油发电量: {report.total_diesel_generated_kwh:.4f} kWh")
    print(f"    柴油机启动次数: {report.total_diesel_starts}")
    print(f"    电池充电量: {report.total_bess_charge_kwh.get('bes1', 0):.4f} kWh")
    print(f"    电池放电量: {report.total_bess_discharge_kwh.get('bes1', 0):.4f} kWh")
    print(f"    初始SOC: {report.initial_soc.get('bes1', 0) * 100:.1f}%")
    print(f"    最终SOC: {report.final_soc.get('bes1', 0) * 100:.1f}%")
    print(f"    甩负荷量: {report.total_load_shed_kwh:.4f} kWh")

    assert len(report.step_records) == report.total_steps
    print(f"  [OK] 逐步记录数量正确: {len(report.step_records)} 条")

    assert len(report.cost_curve) == report.total_steps
    print(f"  [OK] 成本曲线点数正确: {len(report.cost_curve)} 个点")

    assert "bes1" in report.soc_curve
    assert len(report.soc_curve["bes1"]) == report.total_steps
    print(f"  [OK] SOC曲线点数正确: {len(report.soc_curve['bes1'])} 个点")

    print()


def test_real_state_unaffected():
    print("=" * 60)
    print("测试 3: 仿真不影响真实系统状态")
    print("=" * 60)

    real_state = MicrogridState()
    initial_soc = 0.7
    initial_dispatch_count = len(real_state.dispatch_history)
    real_state.bess_state["bes1"].soc = initial_soc

    sim_engine = SimulationEngine(real_state)

    scenario_data = {
        "name": "状态隔离测试",
        "duration_hours": 2,
        "time_step_minutes": 60,
        "pv_series": {
            "pv1": {"segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 100.0}]},
            "pv2": {"segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 100.0}]},
        },
        "load_series": {
            "segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 500.0}],
        },
    }

    scenario = sim_engine.create_scenario(scenario_data)
    report = sim_engine.run_simulation(scenario.scenario_id)

    assert report is not None
    assert report.status == SimulationStatus.COMPLETED

    assert abs(real_state.bess_state["bes1"].soc - initial_soc) < 1e-9, \
        f"真实SOC被改变! 初始: {initial_soc}, 当前: {real_state.bess_state['bes1'].soc}"
    print(f"  [OK] 真实电池SOC未受影响: {real_state.bess_state['bes1'].soc * 100:.1f}%")

    assert len(real_state.dispatch_history) == initial_dispatch_count, \
        f"真实调度历史被改变! 初始: {initial_dispatch_count}, 当前: {len(real_state.dispatch_history)}"
    print(f"  [OK] 真实调度历史未受影响: {len(real_state.dispatch_history)} 条")

    print()


def test_simulation_comparison():
    print("=" * 60)
    print("测试 4: 仿真结果对比")
    print("=" * 60)

    real_state = MicrogridState()
    sim_engine = SimulationEngine(real_state)

    sunny_data = {
        "name": "晴天场景",
        "duration_hours": 4,
        "time_step_minutes": 60,
        "pv_series": {
            "pv1": {"segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 100.0}]},
            "pv2": {"segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 100.0}]},
        },
        "load_series": {
            "segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 300.0}],
        },
    }

    cloudy_data = {
        "name": "阴天场景",
        "duration_hours": 4,
        "time_step_minutes": 60,
        "pv_series": {
            "pv1": {"segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 20.0}]},
            "pv2": {"segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 20.0}]},
        },
        "load_series": {
            "segments": [{"start_minute": 0, "end_minute": 240, "value_kw": 300.0}],
        },
    }

    sunny_scenario = sim_engine.create_scenario(sunny_data)
    cloudy_scenario = sim_engine.create_scenario(cloudy_data)

    sunny_report = sim_engine.run_simulation(sunny_scenario.scenario_id)
    cloudy_report = sim_engine.run_simulation(cloudy_scenario.scenario_id)

    assert sunny_report.status == SimulationStatus.COMPLETED
    assert cloudy_report.status == SimulationStatus.COMPLETED
    print(f"  [OK] 两个场景仿真完成")
    print(f"       晴天成本: {sunny_report.total_cost:.2f} 元")
    print(f"       阴天成本: {cloudy_report.total_cost:.2f} 元")

    comparison = sim_engine.compare_simulations(sunny_report.simulation_id, cloudy_report.simulation_id)
    assert comparison is not None
    print(f"  [OK] 对比报告生成成功")

    print()
    print("  === 差异对比 (晴天 - 阴天) ===")
    print(f"    成本差: {comparison.cost_diff:.2f} 元")
    print(f"    购电量差: {comparison.grid_import_diff:.2f} kWh")
    print(f"    售电量差: {comparison.grid_export_diff:.2f} kWh")
    print(f"    柴油启动差: {comparison.diesel_starts_diff} 次")
    print(f"    柴油发电差: {comparison.diesel_generated_diff:.2f} kWh")
    print(f"    电池循环差: {comparison.bess_cycles_diff.get('bes1', 0):.4f} 次")
    print(f"    最终SOC差: {comparison.final_soc_diff.get('bes1', 0) * 100:.1f}%")

    assert comparison.cost_diff < 0, "晴天成本应该低于阴天"
    print(f"  [OK] 晴天确实比阴天成本更低 (差异 {abs(comparison.cost_diff):.2f} 元)")

    print()


def test_step_records_detail():
    print("=" * 60)
    print("测试 5: 逐步记录详情验证")
    print("=" * 60)

    real_state = MicrogridState()
    sim_engine = SimulationEngine(real_state)

    scenario_data = {
        "name": "逐步记录测试",
        "duration_hours": 1,
        "time_step_minutes": 20,
        "pv_series": {
            "pv1": {"segments": [{"start_minute": 0, "end_minute": 60, "value_kw": 50.0}]},
            "pv2": {"segments": [{"start_minute": 0, "end_minute": 60, "value_kw": 50.0}]},
        },
        "wt_series": {
            "wt1": {"segments": [{"start_minute": 0, "end_minute": 60, "value_kw": 0.0}]},
        },
        "load_series": {
            "segments": [{"start_minute": 0, "end_minute": 60, "value_kw": 150.0}],
        },
        "initial_soc_override": {"bes1": 0.8},
    }

    scenario = sim_engine.create_scenario(scenario_data)
    report = sim_engine.run_simulation(scenario.scenario_id)

    assert report.status == SimulationStatus.COMPLETED
    assert len(report.step_records) == 3

    print(f"  [OK] 仿真完成，{len(report.step_records)} 步记录")

    for i, step in enumerate(report.step_records):
        print()
        print(f"  --- 第 {i} 步 (场景分钟 {step.scenario_minute}) ---")
        print(f"    光伏出力: pv1={step.pv_output.get('pv1', 0):.1f}kW, pv2={step.pv_output.get('pv2', 0):.1f}kW")
        print(f"    风机出力: wt1={step.wt_output.get('wt1', 0):.1f}kW")
        print(f"    柴油机: {step.diesel_output.get('ds1', 0):.1f}kW")
        print(f"    电池: 充电 {step.bess_charge_kw.get('bes1', 0):.1f}kW, 放电 {step.bess_discharge_kw.get('bes1', 0):.1f}kW")
        print(f"    SOC: {step.bess_soc_before.get('bes1', 0) * 100:.1f}% -> {step.bess_soc_after.get('bes1', 0) * 100:.1f}%")
        print(f"    购电: {step.grid_import_kw:.1f}kW, 售电: {step.grid_export_kw:.1f}kW")
        print(f"    负荷: 供应 {step.load_served_kw:.1f}kW, 甩负荷 {step.load_shed_kw:.1f}kW")
        print(f"    成本: {step.step_cost:.4f} 元")
        print(f"    电价时段: {step.tariff_period}")

    first_step = report.step_records[0]
    assert first_step.pv_output["pv1"] == 50.0
    assert first_step.pv_output["pv2"] == 50.0
    assert first_step.scenario_minute == 0
    print()
    print(f"  [OK] 第0步光伏出力正确")

    last_step = report.step_records[-1]
    assert last_step.scenario_minute == 40
    print(f"  [OK] 最后一步时间点正确")

    print()


def test_undefined_source_no_fault():
    print("=" * 60)
    print("测试 6: 未定义时序曲线的源不触发故障预案")
    print("=" * 60)

    real_state = MicrogridState()
    real_state.bess_state["bes1"].soc = 0.5
    sim_engine = SimulationEngine(real_state)

    scenario_data = {
        "name": "只定义光伏，不定义风机",
        "description": "场景中只定义pv1、pv2，不定义wt1",
        "duration_hours": 2,
        "time_step_minutes": 30,
        "pv_series": {
            "pv1": {
                "segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 50.0}]
            },
            "pv2": {
                "segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 50.0}]
            }
        },
        "load_series": {
            "segments": [{"start_minute": 0, "end_minute": 120, "value_kw": 200.0}]
        },
        "diesel_available": {"ds1": True},
    }

    scenario = sim_engine.create_scenario(scenario_data)
    print(f"  [OK] 创建场景: {scenario.name}")
    print(f"       已定义源: pv1, pv2 (未定义: wt1")

    report = sim_engine.run_simulation(scenario.scenario_id)
    assert report is not None
    assert report.status == SimulationStatus.COMPLETED

    print(f"  [OK] 仿真完成: {report.simulation_id}")
    print(f"       总成本: {report.total_cost:.2f} 元")
    print(f"       柴油机启动次数: {report.total_diesel_starts}")

    has_fault_notes = False
    fault_triggered = False

    for step in report.step_records:
        for note in step.notes:
            if "故障" in note or "fault" in note.lower() or "掉线" in note or "启动柴油机" in note:
                print(f"       警告: 步骤 {step.step_index} 出现故障相关记录: {note}")
                has_fault_notes = True
            if "启动柴油机" in note and "50" in note:
                fault_triggered = True

    assert not fault_triggered, "检测到因未定义源触发了柴油机启动(50元启动费)!"
    print(f"  [OK] 未定义wt1未触发故障预案，也没有产生柴油机启动费")

    assert len(report.step_records) > 0
    first_step = report.step_records[0]

    assert "wt1" in first_step.wt_output
    assert first_step.wt_output["wt1"] == 0.0
    print(f"  [OK] wt1出力正确为0，未标记为故障")

    print()


def main():
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "        多场景仿真与调度策略回测引擎 - 单元测试          ".ljust(59) + "║")
    print("╚" + "═" * 58 + "╝")
    print()

    tests = [
        test_scenario_crud,
        test_simulation_execution,
        test_real_state_unaffected,
        test_simulation_comparison,
        test_step_records_detail,
        test_undefined_source_no_fault,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {test_func.__name__}: {e}")
            import traceback
            traceback.print_exc()
            print()

    print("=" * 60)
    print(f"测试完成: {passed} 通过, {failed} 失败")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
