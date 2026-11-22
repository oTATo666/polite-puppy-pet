# Agent 插件 SDK v2 说明（AG-9）

> 面向对象：想往 `plugins\` 里放自己插件的人。核心版本 **1.1.0**。

## 1. 最小插件

```python
# -*- coding: utf-8 -*-
from puppy_agent.core.plugin import Plugin, PluginManifest, ToolSpec, RiskLevel

class MyPlugin(Plugin):
    manifest = PluginManifest(
        id="my_plugin", name="我的插件", version="1.0.0",
        description="干一件事", permissions=["data:read"],
    )
    def setup(self, ctx):
        self.ctx = ctx
    def tools(self):
        return [ToolSpec(
            name="my_tool", description="说明", plugin_id=self.manifest.id,
            risk=RiskLevel.LOW,
            parameters={"type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": ["x"]},
            invoke=self._run,
        )]
    def _run(self, args):
        return {"ok": True, "result": args.get("x")}
```

把它存成 `plugins\my_plugin.py`，在设置 → Agent → 插件启用列表里打开它。

## 2. SDK v2 新增字段（都带默认值，**旧插件不用改**）

| 位置 | 字段 | 默认 | 用途 |
|---|---|---|---|
| `ToolSpec` | `permissions: tuple` | `()` | 声明需要的权限，静态检查用 |
| `PluginManifest` | `permissions: tuple` | `()` | 插件级权限声明 |

核心版本 `CORE_VERSION = "1.1.0"`；`core_version_ok(min_core)` 判断兼容：
声明 `min_core_version <= 1.1.0` 的插件都能加载。

## 3. 风险等级（冻结契约，不可自行绕过）

| 等级 | 行为 |
|---|---|
| `LOW` | 直接执行 |
| `MEDIUM` | 先给用户确认令牌，带令牌才执行 |
| `HIGH` | 确认 + （若 `snapshot=True`）执行前快照，可回滚 |

- `guard(args) -> str`：返回非空字符串 = **硬拒**，连确认机会都不给；
- `risk_fn(args) -> RiskLevel`：按参数动态升降风险（例如沙盒外读文件升 MEDIUM）；
- `confirm_token` 绑定参数哈希、TTL 300 秒、一次性；这些语义由
  `tests/test_agent_contract.py` 冻结，改动会立刻红。

## 4. 信任清单

- 包内自带插件（`puppy_agent\plugins\`）随发布一起构建与检查 → 默认信任；
- 用户目录（`exe 旁边\plugins\`）→ **默认禁用**：
  `trust.json` 里没有记录就不加载；用户在设置页逐个启用；
- 启用时绑定文件 SHA256，**内容一变自动禁用**（防"启用一次后被偷偷换掉"）；
- 插件文件被禁用后，其已注册工具会从注册表**卸下**，不是只在加载时跳过。

## 5. 静态检查

```powershell
python scripts\plugin_lint.py
```

检查项：manifest 必需字段、ToolSpec schema 合法、`permissions` 是字符串列表、
风险等级合法、`guard`/`risk_fn` 可调用、无 `__import__` 式动态加载等。
退出码 0 = 全部通过。发布前会对 `plugins\` 目录下每个文件跑一遍。

## 6. 返回值约定

插件的 `invoke(args)` 必须返回 dict，至少带 `"ok": bool`。常用可选字段：

| 字段 | 含义 |
|---|---|
| `message` / `result` | 给人看的结果或错误信息 |
| `need_confirm` + `confirm_token` | 需要确认（由安全层生成，插件不用自己造） |
| `denied` | 硬拒标记 |
| `untrusted` | 内容是外部数据（文件/网页），规划器据此标污点 |
| `snapshot_id` | 快照 ID（由安全层生成） |

**不要**在插件里起线程或弹窗：插件必须能无副作用地加载与卸载。
