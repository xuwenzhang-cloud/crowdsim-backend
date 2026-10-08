# ARDE 应急双层优化：使用与对接说明

本目录是 `crowdsim-backend` 仓库内的 **ARDE 应急双层优化模块**。
论文全称：**Adaptive Regulation via Dual-Layer Evolution（ARDE）**；外滩机制迁移目标版称 **ARDE-Bund**。

**现状作用：** 读决策评估结果，算出「谁先管、分区做什么、何时升级」，再把汇总参数交给仿真执行。
**机制缺口：** 当前「上层/下层」主要是组织分工输出，尚未实现「外层调治理参数 → 内层个体学习 → 观测反馈」的论文闭环。详见：

- `docs/ARDE对接详细文档.md`（工程对接）
- `docs/ARDE_外滩计算实验接入设计.md`（目标机制与计算实验）

工作目录为 `crowdsim-backend` 仓库根。推荐先执行 `python -m pip install -e .`；
MACE 也可通过 `ARDE_BACKEND_PATH` 指向该仓库根目录。

---

## 1. 怎么用

### 1.1 日常：给前端优化页算方案

仿真 WebSocket 同时只允许一个前端连接，所以优化服务单独开 HTTP：

```bash
cd crowdsim-backend
python -m arde_emergency.serve --host 127.0.0.1 --port 8767
```

健康检查：浏览器或 curl 访问 `http://127.0.0.1:8767/health`
算方案：`POST http://127.0.0.1:8767/optimize`，body 见 `examples/sample_optimize_request.json`。
调控对照：`GET http://127.0.0.1:8767/effectiveness`（读 `examples/demo_effectiveness_result.json`）。

前端开发时 Vite 已把 `/api/crowdsim/arde` 转到 `8767`，页面里不用写死端口。
调控效果页路由：`/home/crowdsim/expAnalysis/ardeEffectiveness`。

### 1.2 不启服务：命令行试一次

```bash
cd crowdsim-backend
python -m arde_emergency.optimizer < arde_emergency/examples/sample_optimize_request.json
```

标准输出是完整优化 JSON。重点看：

- `process` / `actions` / `execution`：给优化页展示
- `arde.aggregatedPolicy`：给仿真 `apply_policy` 用

### 1.3 验证策略有没有进仿真

需要外滩场景文件（`scenarios/shanghai_bund/` 或 bundled backend 下同名目录）：

```bash
cd crowdsim-backend
python -m arde_emergency.demo_effectiveness
```

同一事件下对比：无干预、仅观察、警力引导、临时分流、ARDE。
结果写在 `examples/demo_effectiveness_result.json` 和 `examples/demo_effectiveness.md`。

### 1.4 和页面一起跑（完整链路）

1. 启动仿真后端（`ws://127.0.0.1:8765`）
2. 启动本目录 HTTP（`8767`）
3. 前端 `npm run dev`（`http://localhost:8080`）
4. 事中流程：触发事件 → 决策生成 → 决策评估 → **决策优化选「双层自治理优化」** → 写入优化方案
5. 写入时会下发 `event_decision`；若当时仿真未连接，回到沉浸式仿真连上后会自动补发。

---

## 2. 目录里有什么

```text
arde_emergency/
├── README.md                 # 本说明
├── __init__.py               # 对外导出 build_arde_optimization / resolve_arde_policy 等
├── __main__.py               # python -m arde_emergency → 启动 serve
├── optimizer.py              # 双层求解（上层统筹 + 下层分区动作）
├── policy_registry.py        # arde_* 策略表，字段对齐仿真 self.policy
├── serve.py                  # HTTP：/health /optimize /policies
├── demo_effectiveness.py     # 仿真对照实验
└── examples/
    ├── sample_optimize_request.json      # 标准输入样例（评估+工作流）
    ├── demo_effectiveness_result.json    # 对照实验原始结果
    └── demo_effectiveness.md             # 对照实验可读记录
```

| 文件 | 内容 |
|------|------|
| `optimizer.py` | 读 `evaluation` + `workflow`；判定场景剖面（人群 / 内涝 / 复合）和阶段（warning / response / recovery）；上层出优先级和警力分配；下层出分区动作；汇总成 `aggregatedPolicy`。入口函数：`build_arde_optimization(data, simulator=None)`。 |
| `policy_registry.py` | 五套 decision：`arde_warning`、`arde_crowd_response`、`arde_flood_response`、`arde_combined_response`、`arde_recovery`。`resolve_arde_policy()` 生成仿真 policy 字典；`apply_payload_overrides()` 用前端 payload 覆盖强度、分流、排水等；`event_control_factor()` 告诉仿真该策略把事件压到多强。 |
| `serve.py` | 给前端用的 HTTP。`POST /optimize` 调 `build_arde_optimization`；`GET /effectiveness` 返回对照实验缓存；`GET /sample-request` 返回样例输入。 |
| `demo_effectiveness.py` | 加载外滩路网，按臂施加不同 decision，比较速度/拥堵/水深/风险代理。 |
| `examples/` | 输入样例和实验产出，不是运行时代码。 |

本目录与 `crowdsim/` 仿真引擎及 `scenarios/` 路网共同保存在本仓库；Vue 页面和
MACE 实验编排仍分别属于前端仓库和 MACE 仓库。

---

## 3. 和后端怎么对接

仿真真相在 Python：`OverlayNetworkSimulator.apply_policy()`。
推荐文件（功能更全）：

`MACE-FE2-main/frontend/src/views/crowdSim/backend/crowdsim_overlay_server.py`

独立版：`platform/crowdsim_overlay_server.py`（同样已挂 ARDE）。

中间有一层薄封装，模仿已有的 `decision_evaluation_engine.py`：

- `platform/arde_emergency_engine.py`
- `MACE-FE2-main/frontend/src/views/crowdSim/backend/arde_emergency_engine.py`

overlay 从这里 import：`build_arde_optimization`、`resolve_arde_policy`、`apply_payload_overrides`、`event_control_factor`。

### 3.1 算方案：WebSocket action

仿真已占用前端连接时，优先用 HTTP（见 1.1）。若走 WS：

```json
{
  "action": "run_arde_optimization",
  "requestId": "任意字符串",
  "evaluation": { },
  "workflow": { },
  "experimentPayload": { },
  "options": { "emergencyProfile": "combined" }
}
```

返回 `type: "arde_optimization_result"`，结构与 HTTP `/optimize` 相同。

### 3.2 执行方案：`event_decision` → `apply_policy`

前端下发（或实验脚本直接调）：

```json
{
  "action": "event_decision",
  "decision": "arde_combined_response",
  "payload": {
    "containmentLevel": 92,
    "diversion": 0.86,
    "water_decay_per_second": 0.04,
    "capacity_multiplier": 2.26,
    "speed_recovery": 0.52,
    "visual_relief": 0.8
  }
}
```

`apply_policy` 行为：

1. `decision` 若是 `arde_*`，用 `resolve_arde_policy` 填 `self.policy`（与原有 `police_guidance` 同一套字段：`strength`、`capacity_multiplier`、`diversion`、`water_decay_per_second` 等）。
2. `payload` 再覆盖数值（`containmentLevel` 按百分数转 0–1）。
3. `_event_strength` 用 `event_control_factor`：`arde_crowd_response` / `arde_flood_response` / `arde_combined_response` 与警力引导同档（0.35）。

仿真仍是 **全场一套 policy**，不能按「观景平台 / 廊道 / 地铁口」分别改路段。分区动作写在优化 JSON 的 `arde.lowerLayer` 里给页面看；真正进引擎的是汇总后的 `aggregatedPolicy`。

### 3.3 后端需要认的 decision 名

| decision | 何时出现 |
|----------|----------|
| `arde_warning` | 预警阶段 |
| `arde_crowd_response` | 人群聚集响应 |
| `arde_flood_response` | 内涝响应 |
| `arde_combined_response` | 聚集 + 积水 |
| `arde_recovery` | 恢复阶段 |

原有三种 `police_guidance` / `temporary_diversion` / `observe_only` 仍保留，未删。

---

## 4. 和前端怎么对接

### 4.1 页面与文件

| 前端位置 | 对接内容 |
|----------|----------|
| `src/api/crowdsim/ardeOptimizationClient.js` | 先 `POST /api/crowdsim/arde/optimize`，失败再 WS `run_arde_optimization` |
| `src/api/crowdsim/ardeEffectivenessClient.js` | `GET /effectiveness` 对照结果；一键 optimize + 写入 plan |
| `src/api/crowdsim/ardePolicyDispatch.js` | 读 localStorage 里的 `aggregatedPolicy`，发 `event_decision`；仿真未连则写 pending |
| `src/views/crowdSim/DuringAnalysis/DecisionOptimization.vue` | 方法 ID **`bilevel-autonomy`**（文案：双层自治理优化）。确认方法时调优化客户端；写入方案时调下发 |
| `src/views/crowdSim/DuringAnalysis/ArdeEffectiveness.vue` | **调控效果台**：策略对照图/表、ARDE 前后变化、双层方案与下发 |
| `src/views/crowdSim/DuringExperimentReport.vue` | 「下发 ARDE 策略到仿真」 |
| `src/views/crowdSim/CrowdSimSimulationImmersive.vue` | `arde_*` 不再折成三种旧 preset；连上后执行 pending |
| `src/views/crowdSim/DuringAnalysis/DecisionGeneration.vue` | `resolveBackendDecision` 若已是 `arde_*` 则原样下发 |
| `frontend/vite.config.js` | `/api/crowdsim/arde` → `http://127.0.0.1:8767` |

### 4.2 数据流

```text
决策评估页
  → localStorage: crowdsim_during_evaluation_results
                  crowdsim_during_workflow_instance

决策优化页（bilevel-autonomy）
  → POST /api/crowdsim/arde/optimize
  → 用返回的 process / actions / expectedMetricRows / score 填工作台
  → 写入 crowdsim_during_optimization_plan（含 arde.aggregatedPolicy）
  → dispatchArdePolicyToSimulation()
        ├─ 沉浸式已连接：window.__crowdSimBackendSend(event_decision)
        └─ 未连接：crowdsim_during_arde_pending，仿真 init 后自动 apply

决策报告页
  → 读同一份 optimization_plan 展示；可再次下发
```

### 4.3 前端必须吃的返回字段

优化服务返回需能直接进 `DecisionOptimization.vue`：

| 字段 | 页面用途 |
|------|----------|
| `process[]` 的 `stage/title/desc/calculation/output` | 优化步骤 |
| `actions[]` 的 `index/title/problem/method/effect` | 分区动作卡片 |
| `execution[]` 的 `type/title/desc` | 执行版本 |
| `expectedMetricRows[]` 的 `key/label/current/target/direction` | 指标表和柱状图 |
| `targets[]` | 工作台目标格（`current → target`） |
| `score` | 方案得分 |
| `arde.aggregatedPolicy` | 下发仿真，不直接画在卡片上 |

输入沿用评估页已有对象，不要另造类型：`evaluation`、`workflow`、`experimentPayload`。

### 4.4 方法 ID 约定

前端四种优化方法里，**只有** `bilevel-autonomy` 调本目录。
其它方法（多目标、滚动时域、规则阈值）仍用页面内系数，不走 ARDE。

---

## 5. 对接时不要改错的地方

- 本目录可整体拷贝给其他人；要在 **本仓库 CrowdSim** 里跑通，需同时有上面的 overlay 与前端文件。
- 不要把 `aggregatedPolicy.decision` 映射回 `police_guidance`，否则 ARDE 参数进不了引擎。
- 8767 没开、8765 又被仿真占着时，优化页会提示失败并回退页面 mock。看起来像「没接上」，先查两个服务。
- 分区动作是方案说明；地图上的行人/积水只受全局 `self.policy` 影响。
