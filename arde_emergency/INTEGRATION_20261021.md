# ARDE 最小接入与本地验收（2026-10-21）

## 20261021.4 同场景边界审计

仅修改 ARDE 专属集成，不修改公共仿真回执或人群行为。健康接口增加 `ardeFairComparison=audit-v1`，流程版本更新为 20261021.4。每次 ARDE 重置核对已接受需求指纹、人数、请求种子、生成数量；记录启动前事件应用回执和控制前首帧。请求的种子不等于仿真器实际种子的证明。

报告按种子核对场景、事件与首帧边界。人工暂停/继续/停止/手工动作、模型回退、非 accepted 模型记录和缺失/不匹配回执均列出排除理由，不能混入正常配对。旧实验没有审计证据时不自动补造。方法卡片仍保留全部完成运行的描述性数值。

记录峰值密度时刻及能够对应相同测量值的道路/区域，找不到位置则明确未知；关联峰值时刻或之前的决策、动作回执，以及配置评价窗口前后的全局/区域观测。窗口不足不补零；同帧决策发生在该帧观测之后，不能用它解释该帧峰值；前后变化不是因果证据。区分生成干预、已应用干预和仅观测动作。

用户明确要求暂不修改公共仿真证据接口，因此 `runtimeSeedVerified=false`、`fullInitialPopulationVerified=false`，核对等级为 `boundary-only`。页面显示“初始条件未完全验证”，配对差值仅作描述，不能将边界通过宣称为完整初始人群相同的证明。本地测试不等于部署后的真实 SUMO 一致性验收；LLM 密钥和零疏散原因仍需独立排查。

## 版本与边界

前端基于 origin/main 5533679，当前分支 arde/integration-20261021；旧三个 ARDE 提交由标签 arde-backup-110fbf9-20261021 保留。后端 main 为 4295ef1。没有新仓库或 worktree，没有推送，没有运行正式矩阵。

前端唯一修改的既有共享文件是 frontend/src/router/index.js：将 CrowdSim 决策生成组件换为 DecisionGenerationHub。其余新增文件全部属于 ARDE 页面、组件、客户端、状态与测试。

后端仅新增 arde_emergency/experiment_service.py、arde_emergency/experiment_report.py 和 ARDE 测试/说明；没有修改原编排、合同、控制器注册、仿真器或其他同学的业务代码。

## 完整事中流程对照

1. 经典需求入口：RequirementDefinition.vue → requirementClient.js 提交 → 仿真器 requirement_accepted → useCrowdSimRequirementStore.accept。ARDE 直接读取这个已接受快照，拒绝 req-default 调试 ID。
2. 新版对话入口：useDuringExperiment.js → createDuringExperimentClient → createUnifiedRequirementWorkflow → 澄清、核对、确认、保存冻结快照 → submitRequirementSnapshot → /requirements HTTP → 后端接受回执。该流程的已接受需求保存在 crowdsim.requirement.v3.<conversationId>，并不自动进入经典需求仓库。ARDE 新增只读来源选择，仅展示冻结快照且存在后端回执的条目，由用户明确选定后复制进需求仓库，不修改原对话代码。
3. 新版原有预设实验：duringExperimentWorkflow / duringExperimentModel → presetId → contracts.bind_preset_scenario → C0 推演；during_strategy 仅接受 LLM 固定策略并创建 D2 确认重跑。此路径保留，不放宽预设合同，不把 C5 冒充 D2。
4. 决策生成：路由 → DecisionGenerationHub；普通模式原样加载 DecisionGeneration.vue，?mode=arde 加载 ArdeControlExperiment.vue。不增加左侧导航或独立 /ardeControl。
5. ARDE 创建：已接受来源 → ardeExperimentModel 构建独立快照，显式记录本次人数、时长、种子、事件与六种方法 → 专属客户端使用现有 /crowdsim-control-api/crowdSim/control 代理。REST 和 fetch SSE 使用相同 user/workspace，避免原生 EventSource 无法传工作区头造成读取错位。
6. 服务校验：前端先检查 health.ardeRequirementBinding=snapshot-v1。旧服务不能忽略新字段后继续开跑；缺少绑定能力时前端阻止新矩阵创建和启动。已有历史结果仍可查看。
7. ARDE 运行：专属服务继承现有 orchestrator；仅对带 ardeRequirementSnapshot 的实验新增绑定。每个方法、每个种子均在现有网关租约内重新提交同一需求快照，核对 requirement_accepted 的指纹，然后使用返回的 requirement_id 重置仿真。非 ARDE 实验委托原逻辑。
8. C0–C5：现有 registry 创建控制器，现有单 worker 队列顺序执行。C5 使用现有 ArdeAdapter。观测由 ObservationBuilder 构建；控制器决定动作；ActionValidator 校验；网关 apply_action；queued 不作为执行成功，最终 applied/rejected 关联 requestId/actionId；效果窗口完成后 on_effect 更新 C5 状态并写入持久化记录。
9. 运行显示：REST 轮询 + SSE 显示状态、观测、决策、回执和评价。新增 ARDE records 读取接口按同一工作区恢复持久化回执/评价，补足服务重启后事件流不含旧事件的问题。重置或离开页面关闭流与计时器，阻止旧异步响应污染新实验。
10. 报告：复用原报告的风险、密度、完成率、T95、动作成功率、模型回退说明和配对比较。ARDE 专属扩展增加观测平均速度、最低观测平均速度及相应同种子比较。缺失值不显示成 0；没有达到 95% 时不伪造 T95。聚合均值差与同种子配对表分别标注。
11. 历史结果：/home/crowdsim/decisionGeneration?mode=arde&stage=comparison&experimentId=<id>。历史加载不需要当前需求，但没有绑定快照的旧实验禁止重新启动，应新建矩阵。

## 本地测试结果

- ARDE 前端针对性测试 6 项通过：需求门禁、六方法正式/冒烟矩阵、严格种子解析、快照隔离、代理/工作区、HTTP 错误、SSE 分片/去重/关闭、普通/ARDE 模式与历史结果、旧服务阻断、对话快照来源。
- 原有事中回归 145 项通过；原组件文件在 Windows 下直接运行会因绝对路径进入 JS import 而报 ERR_UNSUPPORTED_ESM_URL_SCHEME。新增 tests/ardeUpstreamComponents.test.mjs 在临时副本中归一化路径后，原 33 项组件测试通过。原测试未修改。
- 合并测试命令统计 152/152 通过，其中一项包装测试实际执行上述 33 项组件断言。
- ARDE 文件 ESLint 通过。
- 后端 61 项通过：原 ARDE 控制器、原控制实验、绑定回归、逐方法同快照重置、指纹拒绝、非 ARDE 原逻辑、六方法队列、报告速度/回退和本地 HTTP 历史回执/工作区隔离。
- 前端生产构建通过；有原项目包体积及混合导入警告。
- 测试未调用真实 LLM 或启动 SUMO；本地 HTTP 冒烟使用临时端口与测试数据。因此上述结果不是六组真实仿真效果验收。

## 服务器启动准备（本轮未执行）

保留原前端代理配置。将 8766 控制服务的启动模块改为以下 ARDE 入口；同一端口不要同时启动两个服务：

```bash
export CROWDSIM_EXPERIMENT_DIR=/your/persistent/arde-experiments
export CROWDSIM_LLM_BASE_URL=https://your-model-endpoint
export CROWDSIM_LLM_API_KEY='由服务器环境安全注入'
export CROWDSIM_LLM_MODEL=your-model
export CROWDSIM_WS_URL=ws://127.0.0.1:8765
python -m arde_emergency.experiment_service --host 127.0.0.1 --port 8766
```

健康检查仍使用 /crowdSim/control/health，需返回 ardeRequirementBinding=snapshot-v1。真实 SUMO 服务和模型服务可用后，从需求界定提交真实场景，再进入决策生成选择 ARDE。

- 冒烟：选择六方法冒烟，默认收敛为 30–100 人、120 秒、1 个种子、六个运行；可明确调整本次配置，来源快照不被改写。
- 正式：确认同场景 600 人、900 秒、10 个种子，60 个运行；论文统计可用 30 个种子，180 个运行。
- 先完成真实冒烟并查看每个动作终态和最终报告，再决定正式实验。运行由现有单 worker 串行调度，不与其他控制服务共享占用同一个仿真器。
- 模型密钥不进入前端、请求配置或 Git。C2–C4 的 fallback 不能作为真实 LLM 基线；报告保留回退警告和各方法回退计数。
- C5 默认使用现有完整 ARDE 自适应控制器，可选 LLM 指导默认关闭；本轮未伪造或启用该指导器。若论文设计要求 LLM 辅助 ARDE，需另行接入 provider 并验证实际调用，不能将当前实验称为已启用 LLM 指导。

## 仍须服务器验证的事项

真实地图/需求切换、仿真动作能力、全矩阵公平重置、SUMO 动作终态、事件实际影响和效果窗口均需真实冒烟确认。需求中的事件类别在原后端是 metadata_only；ARDE 配置的火灾注入是独立显式开关，不会把任意事件类别自动转换成火灾。速度是观测帧全场平均速度的均值和最小值，不能解读为单个行人的最低速度。

本机既有纪念塔预设的资源校验曾出现 bund.hotspot.sumocfg 指纹不一致；本轮没有改动或绕过该预设校验。ARDE 快照矩阵走正常已接受需求路径，不能因此宣称预设实验或对应资源已通过验证。

## 20261021.2：真实流程测试后的修订

- 页面直接显示流程版本 `20261021.2`；健康检查同时要求 `ardeRequirementBinding=snapshot-v1` 和 `ardeConfigurationReview=review-v1`。仅能连通 HTTP 不再等同于 ARDE 流程可用。
- ARDE 初次进入默认选择六方法冒烟：一个种子、30–100 人、60–120 秒。原需求是 50 人时保持 50 人；原需求超出冒烟范围时，明确展示缩小后的配置并要求确认。正式模式仍需显式切换，至少 10 个种子。
- 已接受来源只取自 submission；不再把页面上重复传入的 requirement 对象当作另一份真实来源。来源需求与本次实验人数/时长分开显示。
- 保存原始来源快照与派生运行快照。人数或来源已指定的时长发生变化时，必须勾选调整确认；再次修改值就撤销旧确认。后端核对准确的调整列表，只允许派生人数和时长，不允许静默替换地点或人群结构。
- 切换来源需求时清除前一来源的火灾选择和调整确认。历史页面恢复原实验的火灾启用状态，且即使当前浏览器没有需求记录，也能查看实验冻结快照。
- 事件类别不会直接触发火灾。火灾需要明确启用，校验事件类别、半径和强度；普通策略生成与独立 ARDE 矩阵的不同前置条件在页面上解释。
- 人群表单未填写项的零值交互属于原需求界定代码，本轮没有修改；本次只修订 ARDE 流程。

验证：前端合并测试 158/158（含原 33 项组件回归的包装测试），后端 ARDE/控制实验测试 67/67，ARDE 相关文件 ESLint 通过。服务器旧页面不能通过修改本地源码自动更新，仍须部署两个仓库的 ARDE 分支并重启对应服务，再执行真实六方法冒烟。
