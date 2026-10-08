# CrowdSim SUMO Backend

该后端以 SUMO 1.24.0 striping 为唯一运动引擎。SUMO 管理行人实际位置、速度、道路交互、出发和到达；Python 管理画像、局部观察、心理状态、信息、策略、危险输入、实验记录和 WebSocket 协议。事件不会直接乘人数或密度。

## 工程结构

```text
crowdsim/
├─ domain/          数据合同、画像、同行关系
├─ core/            仿真生命周期、人口账本、状态更新、命令队列
├─ environment/     局部感知、事件、信息、危险、干预、POI
├─ decision/        规则/LLM接口、调度、路线候选、计划执行
└─ infrastructure/  SUMO/路网适配、WebSocket、帧、指标、实验记录

arde_emergency/     ARDE 状态、双层决策、奖励、动作映射与兼容接口

config/             参数、属性注册表和POI
scenarios/          正式外滩SUMO场景
scripts/            运行、回放和验收工具
tests/              自动测试及仅供测试的SUMO场景
docs/               设计、协议、论文依据和实施记录
runs/               不纳入版本控制的实验产物
```

根目录只保留兼容启动入口 `crowdsim_overlay_server.py`。完整模块职责见 `docs/工程目录与模块边界.md`。

## 环境

- Python 3.12：`D:\Anaconda\python.exe`
- SUMO 1.24.0：设置 `SUMO_HOME`，本机当前为 `D:\sumo-1.24.0`

```powershell
D:\Anaconda\python.exe -m pip install -r requirements.txt
D:\Anaconda\python.exe -m pip install -e .
D:\sumo-1.24.0\bin\sumo.exe --version
```

`pip install -e .` 将仓库内的 `arde_emergency` 安装为可导入包。MACE 可以直接
`from arde_emergency import ArdeController`；若 MACE 与本仓库相邻，也会通过
适配器自动发现。分开部署时，将 `ARDE_BACKEND_PATH` 设置为本仓库根目录。

`traci`、`sumolib` 和 SUMO 二进制必须使用相同版本。仅安装 Python 包不包含完整 SUMO 仿真程序。

## 启动

```powershell
$env:SUMO_HOME='D:\sumo-1.24.0'
D:\Anaconda\python.exe crowdsim_overlay_server.py --host 127.0.0.1 --port 8765
```

默认地址为 `ws://127.0.0.1:8765`。协议见 `docs/websocket_protocol.md`。

默认启动普通外滩研究场景。启动“上海人民英雄纪念塔有限聚集”热点场景时，使用场景预设，让 SUMO 配置与行人路线保持成对选择；该热点预设目前只包含热点访客，不包含背景行人：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --scenario hotspot --host 127.0.0.1 --port 8765
```

热点预设默认生成 700 名访客，也支持通过 `configure.count` 在 0—10000 人范围内重建需求，
或使用已保存需求的 `population.total`（1—10000 人）；该场景不包含背景行人。需求界定详情、
`requirement_id` 及运行快照见 `docs/websocket_protocol.md`。后台按 240—660 s 的三段目标到达曲线
倒推每名访客的刷新时刻，活动在 600 s 开始、1000 s 结束，并在之后 120 s 内逐渐释放离场。
服务向初始化帧输出 0—1800 s 的热点观察时间轴，共 3600 个 0.5 s 步。

如需使用自定义 SUMO 配置，必须同时指定配置中实际引用的行人路线；已内置的两个配置可以省略 `--ped-routes`：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --config scenarios\custom\scenario.sumocfg --ped-routes scenarios\custom\pedestrians.rou.xml
```

使用本地 `pedestrian_decision_skill/config.json` 中的 DeepSeek 配置启动 LLM 决策：

```powershell
D:\Anaconda\python.exe crowdsim_overlay_server.py --mode llm --host 127.0.0.1 --port 8765
```

## 场景预检

```powershell
D:\Anaconda\python.exe scripts\validate_scenario.py --all --duration 30 --output runs\stage1\scenario_validation.json
```

脚本会严格检查 striping、0.5 s 步长、路线、解堵阈值、行人 lane、真实 person 插入及 FCD 位移，并重建四类微型场景。

## 实验与回放

```powershell
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --steps 120 --mode rule
D:\Anaconda\python.exe scripts\run_experiment.py --count 20 --until-finished --mode rule
# 兼容旧内联日志与新拆表日志；报告写入新的回放运行目录
D:\Anaconda\python.exe scripts\replay_experiment.py runs\<source_run_id>
```

运行产物写入 `runs/<run_id>/`，包括 manifest、实际需求、画像、命令、消息、决策、生命周期、轨迹、指标、SUMO 日志和摘要。新决策日志默认使用六个 JSONL 表无损去重，不引入数据库；格式和测试见 [决策日志拆表](docs/decision_log_format.md)。读取器兼容旧格式与新格式，按需解析计划和路线；基础回放按时间批次消费计划与轨迹，不生成新规则或 LLM 决策，并比较位置、速度和人口账本。历史日志不会自动转换或改写。基础回放不支持包含已记录外部命令的实验，也不等于完整 context 复原或所有场景均可精确复现。

## DeepSeek 行人决策 Skill

背景行人 POI 重规划的可达性、合法位置、完整行程执行、重试冷却及诊断说明见 [POI 重规划补强](docs/poi_replanning.md)。功能保持开启，热点访客的锁定行程不被自主 POI 选择覆盖。

`pedestrian_decision_skill` 已实现可注入 `DecisionScheduler` 的 DeepSeek 决策引擎。它读取冻结的画像、状态、周围人群和有限候选，直接生成 `BehaviorPlan`；道路、速度和等待时间均由可信后端数据补齐。非法目标、超时或错误会记录并回退到规则计划。配置缺失时 `enabled=false`，不会把规则结果统计成真实 LLM 决策。详细接口见 `pedestrian_decision_skill/INTEGRATION.md`。

## 测试与验收

```powershell
D:\Anaconda\python.exe -m pytest -q
D:\Anaconda\python.exe scripts\run_acceptance.py
```

`run_acceptance.py` 为 F01—F18 生成逐项状态。缺少真实模型凭据或实际前端联调时，对应项目保持 `partial`，不会把 skip 或模拟返回写成完整通过。

## 系统循环

```text
S(t)冻结快照
  → 事件/消息在边界生效
  → 局部观察与到期决策
  → 规则或LLM生成BehaviorPlan
  → 校验并通过TraCI执行
  → SUMO推进唯一一个0.5s步长
  → 批量更新压力/疲劳与人口账本
  → 计算真实密度/速度/流量并发送S(t+dt)
```

明确不支持运行时任意多边形硬障碍、动态硬封路、火灾/积水物理场、身体接触压力或连续二维群体队形；相关请求必须明确拒绝。
## 上海人民英雄纪念塔热点人群实验

默认实验现在使用“上海人民英雄纪念塔有限聚集”需求，只包含热点访客，不再生成背景行人。需求不是运行中补人，而是在启动前生成包含明确出发、到达、活动停留和离场阶段的 SUMO person。热点访客按三段目标到达曲线倒推出发时间，并按可用长度加权随机刷新到黄浦公园西侧的五条园内人行道路上。由于刷新位置已经属于公园路网，行人不再先折返南、北公园入口，而是直接在园内选择路线前往两个纪念塔入口；进入园内道路后允许根据画像和拥堵重新选择纪念塔入口。访客随后进入地面环道，按最佳、次优和普通观赏弧段的权重选择目标位置，停留到统一活动结束时刻后在 120 s 内逐渐离场，最后经公园道路离开至园外道路：

```powershell
D:\Anaconda\python.exe scripts\generate_hotspot_demand.py
D:\Anaconda\python.exe scripts\run_hotspot_experiment.py
D:\Anaconda\python.exe scripts\run_experiment.py --steps 3600
```

热点规模和时间窗见 `config/crowd_hotspots.json`。`runs/hotspot/people_heroes_monument_report.json` 记录环道人数、密度、入口低速行人、外围速度及后期消散。当前数值是涌现机制演示参数，未经过外滩实测标定。

在线帧把观测区拆成 `core`（外圈两条地面环道边）、`entries`（纪念塔两个入口）、`park`（黄浦公园内部道路）、`park_entries`（公园南北入口）和 `external_approach`（园外接近道路）。内侧地面环道和更深层环道不参与访客目标、路线或核心区测量。当前访客从 `park` 区域开始，`park_entries` 仍用于网络观测和离场过程，不再作为到访必经点。`core_process_state` 描述环道自身的 `normal → building → congested → dispersing → cleared`；安全口径的 `process_state` 还会检查园内、园外未完成访客与低速积压，必要时进入 `residual_congestion`，避免环道清空后误报整体消散。`visit_lifecycle` 给出未出发、园外接近/排队、园内接近/排队、进入环道、聚集停留、离场、完成和未完成人数。其中 `stopped_count` 表示计划停留，`slow_walking_count` 才表示步行阶段的低速人数。
