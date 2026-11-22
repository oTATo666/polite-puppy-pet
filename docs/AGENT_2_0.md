# 礼貌小狗 · Agent 2.0 架构说明（AG-9）

> 本文描述**实际实现**，不描述愿景。第四轮（AG-1~8）完成后写就；
> 任何功能若本文说"支持"但代码里找不到，以代码为准并请提 issue。

## 1. 一句话

Agent 2.0 把原来的「一句话 → 一个工具」升级为
**规划（planner）→ 执行（executor）→ 观察（compressed）→ 再规划**的多步链，
同时保留规则规划作默认与兜底——**规则永远可用，模型只是可选增强**。

## 2. 分层

```
用户一句话
   │
   ▼
planner.plan(text) ── {"tool", "args", "reason"}   ← 统一接口（AG-2）
   │      ├─ RulePlanner      规则，离线，确定性（默认）
   │      └─ ModelPlanner     模型，严格JSON+schema+动词闸+回落（AG-3）
   ▼
Executor.run(text) ── 多步、预算、防循环、污点、挂起/续跑（AG-4）
   │  每步 → runtime.call(tool, args, token, meta)  ← 安全层完全不变
   ▼
SafetyGate.check → 风险分级 + 确认令牌 + 硬拒 + 快照（冻结契约 AG-1a）
   ▼
plugin.invoke → 结果压缩成一句人话 → 回规划器决定下一步
```

**安全语义冻结**：AG-1a 的契约测试（`tests/test_agent_contract.py`）固定了
三级风险、确认令牌 TTL/绑定/一次性、guard 硬拒、快照上限、审计字段、
ToolSpec schema 与 runtime 调度流程。第四轮所有任务都必须让它保持全绿。

## 3. 规划器接口（AG-2 / AG-3）

```python
plan(text: str) -> {"tool": str, "args": dict, "reason": str}
```

- `RulePlanner`：从 `main.py::_agent_plan` **原样搬入**，`main._agent_plan`
  变成兼容壳；`tests/test_planner.py` 用 24 条基线 + 40 条合成用例逐条对照，
  两边返回值必须完全相同。
- `ModelPlanner`：
  - 严格 JSON + schema 校验；未知工具/坏 JSON/注入输出 → 回落规则；
  - MEDIUM/HIGH 工具要求原文带明确动词（防模型自作主张）；
  - 60 秒缓存、每日上限（默认 50）、云端知情同意（默认拒绝）；
  - 本地 8 秒 / 云端 10 秒超时；任何失败都回落，**永不抛异常**；
  - 请求体只有规划系统提示词 + 用户这一句话——不含记忆片段、文件内容、
    私人称呼（`tests/test_model_planner.py` 抓请求体验证）。

## 4. 执行器（AG-4）

- 默认最多 5 步 / 30 秒；同一步骤指纹（tool + 排序后的 args）第二次出现 → 取消剩余；
- 任何一步被安全层硬拒 → 取消剩余；
- HIGH 需确认的步骤 → 计划挂起（可 `/api/agent/resume` 带 plan_id + 令牌续跑，
  10 分钟过期）；挂起时指纹从 seen 摘掉，避免续跑被自己的防循环误杀；
- 只要读过外部数据（`untrusted: true`），后续所有步骤与最终输出标
  `tainted: true`；
- 观察压缩：工具结果只留一句人话进下一步，**文件原文不回灌**。

## 5. 插件与信任（AG-1 / AG-7）

- SDK v2：核心版本 1.1.0；`ToolSpec` / `PluginManifest` 新增
  `permissions`（旧插件不声明也能加载，行为不变）；
- 信任清单：包内自带插件默认信任；用户 `plugins\` 里的默认禁用，
  `trust.json` 绑定哈希，内容一变自动禁用；
- 内置 8 个日常助手工具（AG-7）：时间、四则运算（ast 白名单禁 eval）、
  便签增删查、提醒增删查（静默时段 23:00–07:00 顺延、上限 20 条）、
  app_open（白名单空 = 一律拒绝，且 HIGH 需确认）。

## 6. 状态与审计（AG-6）

- `/api/agent/state`：旧字段（ok/ready/booted/error/version/tools/plugins/
  pending_confirm）全部保留；新增 phase / plan_id / step / steps_total /
  tool / mood_hint / planner。
- `/api/health`：`layers.agent` 出现，旧四层（brain/ollama/cloud/tts）不动。
- 审计行：`runtime.call(meta=...)` 把 plan_id / step / planner / model /
  tainted / latency_ms 并入 JSONL，旧字段（ts/time/tool/args/risk/ok/
  detail/ms）一个不少；不传 meta 时行为与以前一致。

## 7. 设置页与界面预留（AG-8 / UI-0）

- 设置 → Agent：规划模式（off/rule/local/cloud，持久化，无模型时
  local/cloud 置灰）、云端知情勾选、每日上限、插件启用列表、提醒列表、
  app 白名单编辑、执行链相位与计划步骤；
- 高风险确认框明确显示**工具名 + 参数 + 原因**；
- `<html data-skin data-theme data-motion data-agent-phase>` 与
  `puppy:skin-change` / `puppy:theme-change` / `puppy:agent-phase` /
  `puppy:motion-change` 事件已预留；偏好存 localStorage，
  `prefers-reduced-motion` 作为动效初值。**视觉未改**。

## 8. 评测（AG-5）

`tests/agent_eval/cases.json` 159 条合成黄金用例（规则 133 + 模型 26），
`python scripts/agent_eval.py` 跑全量，指标门：规则 100%、模型行为断言
100%、输出 tool 有效性。退出码 0 = 全过，2 = 黄金集不足 150 条。

## 9. 已知边界（如实写）

- 多步链目前只由 `Agent 页` 的「动手」入口与聊天预取触发；模型规划需要
  用户显式切到 local/cloud 并（云端时）勾选知情同意；
- 提醒由前端轮询 `remind_list` 后提示，插件不自行起线程；
- 规划模式改动重启后生效（运行中的 planner 实例不热替换）；
- 模型规划失败一律回落规则——这是特性，不是缺陷；因此"模型更聪明"
  不会以牺牲安全或可用性为代价。
