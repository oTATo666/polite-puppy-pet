# Agent UI 契约（AG-9 / UI-0）

> 前端与后端之间的 Agent 相关约定。UI-0 只做预留，**不改视觉**。

## 1. HTML 根元素 data 属性

`<html>` 上固定四个属性，初值如下：

| 属性 | 初值 | 说明 |
|---|---|---|
| `data-skin` | `default` | 皮肤标识（预留，当前无换肤 UI） |
| `data-theme` | `light` | 明暗主题（预留，当前无切换 UI） |
| `data-motion` | `full` / `reduced` | 动效开关；用户没选过时取 `prefers-reduced-motion` |
| `data-agent-phase` | `idle` | 执行链相位（瞬时状态，不持久化） |

前三项用户选择存 `localStorage["puppy_ui0"]`，第四项每次启动回 `idle`。

## 2. 事件（DOM CustomEvent）

| 事件名 | detail | 效果 |
|---|---|---|
| `puppy:skin-change` | `{skin: string}` | 改 `data-skin`，持久化 |
| `puppy:theme-change` | `{theme: string}` | 改 `data-theme`，持久化 |
| `puppy:motion-change` | `{motion: "full"|"reduced"}` | 改 `data-motion`，持久化 |
| `puppy:agent-phase` | `{phase: string}` | 改 `data-agent-phase`，**不持久化** |

后端状态刷新（`agLoadState()`）会自动派发 `puppy:agent-phase`。

这些事件是**预留接口**：当前没有任何 CSS/JS 根据它们改变视觉；
以后要换肤/深色/减动效时，监听这四个事件即可，不用改后端。

## 3. `/api/agent/state` 字段

旧字段（全部保留、类型不变）：
`ok, ready, booted, error, version, tools, plugins, pending_confirm`（就绪时还有 `audit_lines`）。

AG-6 新增：

| 字段 | 类型 | 含义 |
|---|---|---|
| `phase` | string | `idle` / `planning` / `running` / `awaiting_confirm` / `done` / `error` |
| `plan_id` | string | 当前/最近执行链 ID；空串表示没有 |
| `step` | int | 当前第几步 |
| `steps_total` | int | 已执行的步数 |
| `tool` | string | 当前步骤工具名 |
| `mood_hint` | string | 给立绘的一句轻提示（如"等你点头"） |
| `planner` | string | `off` / `rule` / `local` / `cloud` |

## 4. 设置页 Agent 页 API

| 方法 | 路径 | 请求体 / 返回 |
|---|---|---|
| GET | `/api/agent/config` | `{mode, modes, cloudConsent, dailyLimit, localAvailable, activeMode}` |
| POST | `/api/agent/config` | `{mode?, cloudConsent?, dailyLimit?}` → `{ok, config}` |
| GET | `/api/agent/plugins` | `{ok, items:[{name, dir, enabled, changed}]}` |
| POST | `/api/agent/plugin` | `{name, enabled}` → `{ok}`；内置插件禁用会被忽略 |
| GET | `/api/agent/assistant` | `{ok, reminders:[...], whitelist:[...]}` |
| POST | `/api/agent/assistant` | `{whitelist: [绝对路径, ...]}` → `{ok, count}` |
| POST | `/api/agent/resume` | `{plan_id, confirm_token}` → 执行链结果 |

模式改动**重启后生效**（`activeMode` 表示当前正在用的模式），
同一页会同时显示"已选"和"当前"，避免误以为立刻切换。

## 5. 确认框

高风险动作执行前，`window.confirm` 必须同时显示：

```
工具：<tool 名>
原因：<reason / summary>
参数：<JSON.stringify(args).slice(0, 300)>
```

确认后：
- 有 `plan_id` → POST `/api/agent/resume`；
- 单步路径 → POST `/api/agent/act` 带 `confirm_token`。

取消 = 不做，链取消剩余步骤。
