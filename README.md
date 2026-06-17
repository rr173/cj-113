# 电力微网调度与储能管理服务

基于 Python + Flask 构建的工业园区微电网智能调度系统，实现光伏发电、风力发电、柴油发电机、电池储能系统的最优协调调度。

## 系统架构

```
发电源上报 (REST)
    ├── 光伏阵列1/2 (各100kW)
    ├── 风机1 (50kW)
    ├── 柴油发电机1 (200kW)
    └── 储能系统上报 → [调度决策引擎] → 输出最优分配
负荷上报 (REST)                               ↓
                                        历史记录 + 累计统计
```

## 预置设备配置

| 设备 | 规格 | 参数 |
|------|------|------|
| 光伏阵列 ×2 | 各100kW额定 | 新能源优先消纳 |
| 风机 ×1 | 50kW额定 | 新能源优先消纳 |
| 柴油发电机 ×1 | 200kW额定 | 启动固定成本50元，发电2元/kWh |
| 电池储能 | 500kWh / 100kW充放 | SOC 20%-90%，充电95%/放电92%效率 |
| 柴油机约束 | 最小运行30分钟 | 停机冷却15分钟才能再启动 |

## 分时电价

| 时段 | 时间 | 电价 (元/kWh) |
|------|------|--------------|
| 谷时段 | 23:00 - 07:00 | 0.4 |
| 平时段 | 07:00 - 11:00, 15:00 - 23:00 | 0.8 |
| 峰时段 | 11:00 - 15:00 | 1.2 |
| 上网电价 | 固定 | 0.3 |

## 调度决策逻辑

1. **新能源优先**：先计算光伏+风电总出力
2. **缺口填补顺序**：电池放电（SOC>下限）→ 比较购电单价 vs 柴油发电成本，选便宜的
3. **盈余消纳顺序**：电池充电（SOC<上限）→ 余电上网
4. **柴油机约束**：启动后最少运行30分钟，停机冷却15分钟
5. **甩负荷**：所有源最大出力仍不足时，告警并记录缺口
6. **电池SOC**：按效率折算，每次充放电后持续追踪更新

## 安装启动

```bash
pip install -r requirements.txt
python app.py
```

服务地址: `http://127.0.0.1:5000`

## 核心接口

### 1. 数据上报

**发电源出力上报**
```bash
curl -X POST http://127.0.0.1:5000/api/source/report \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "pv1",
    "source_type": "pv",
    "power_kw": 85.5,
    "available": true
  }'
```
- `source_type`: `pv`(光伏) / `wt`(风电) / `diesel`(柴油机)

**负荷上报**
```bash
curl -X POST http://127.0.0.1:5000/api/load/report \
  -H "Content-Type: application/json" \
  -d '{"load_kw": 350.2}'
```

所有源+负荷都上报后自动触发调度决策。

### 2. 状态查询

| 接口 | 说明 |
|------|------|
| `GET /api/status/sources` | 各发电源实时状态和出力 |
| `GET /api/status/bess` | 电池SOC和充放电状态 |
| `GET /api/status/tariff` | 当前时段电价及完整电价表 |
| `GET /api/status/load` | 当前负荷及新能源总出力 |
| `GET /api/health` | 服务健康检查 |

### 3. 调度历史 & 统计

| 接口 | 说明 |
|------|------|
| `GET /api/dispatch/history?limit=50&offset=0` | 调度决策历史（含时间戳、出力分配、电池动作、成本） |
| `GET /api/stats/accumulated` | 累计统计：发电量分源、购售电、柴油机启动、总成本 |
| `GET /api/alerts?limit=100` | 告警记录（含甩负荷等） |

### 4. 配置修改（立即生效）

**修改电价**
```bash
curl -X PUT http://127.0.0.1:5000/api/config/tariff \
  -H "Content-Type: application/json" \
  -d '{
    "valley": {"price": 0.35},
    "peak": {"price": 1.15},
    "feed_in": 0.35
  }'
```

**修改电池SOC工作区间**
```bash
curl -X PUT http://127.0.0.1:5000/api/config/bess_soc \
  -H "Content-Type: application/json" \
  -d '{"bes_id": "bes1", "soc_min": 0.15, "soc_max": 0.95}'
```

### 5. 手动触发调度

```bash
curl -X POST http://127.0.0.1:5001/api/dispatch/trigger
```

## 运行测试

```bash
# 先启动 app.py
python test_flow.py
```

测试脚本覆盖 8 个场景：新能源充足充电、平时段外购电、峰时段柴油机、配置修改、甩负荷告警、历史查询、累计统计、告警记录。

## 调度决策返回字段说明

```json
{
  "pv_output_kw": {"pv1": 80, "pv2": 85},
  "wt_output_kw": {"wt1": 30},
  "diesel_output_kw": {"ds1": 0},
  "bess_action": {
    "bes1": {
      "charge_kw": 45.0,
      "discharge_kw": 0,
      "soc_before": 0.5,
      "soc_after": 0.508
    }
  },
  "grid_import_kw": 0,
  "grid_export_kw": 30.0,
  "load_served_kw": 120,
  "load_shed_kw": 0,
  "decision_cost": -9.0,
  "tariff_period": "flat",
  "grid_buy_price": 0.8,
  "notes": ["电池充电 45.00kW", "余电上网 30.00kW (收入 9.00元)"]
}
```
