# 桌宠 Agent · 安全层与可插拔架构 落地记录（P5）

写于 2026-10-27。承接《记忆 P0 · 交接与验收》。
**人格画像已导入、记忆库已导入（13733 条）、Agent 安全层与插件架构已实现并通过验收。**

---

## 0. 一句话结论

桌宠现在不只是"会聊天的狗"了，它有了一个**能动手干活的 Agent 内核**：
一套可插拔的工具（文件、记忆库、消息代发）、三级风险管控 + 确认令牌 + 快照回滚的**安全层**、
以及一份从她真实发言里统计出来的**人格画像**。
人格只管"怎么说"，安全层管"做不做"——**人格无权否决执行**。

---

## 1. 新增的东西

### 1.1 人格画像（`persona.json`）

从她真实聊天记录里统计出来的，不是编的。

| 字段 | 内容 |
|---|---|
| `identity` | 身份设定：礼帽小狗、什么时候开始、怎么称呼对方 |
| `style` | 说话风格：短句、口语、爱用语气词 |
| `catchphrases` | 口癖（从发言里抽的高频口头禅） |
| `emotion_tags` | 情绪标签（开心/委屈/撒娇…） |
| `rhythm` | 说话节奏：短句为主、一句一逗 |
| `execution_layer` | 执行层约定：只管说、不擅自动手 |

注入方式**分两层**，避免重复：

- **基础提示词（原有身份段）** 一直带着，负责"她是谁"；
- **数据片段（新增，≤260 字）** 由 `persona_data_block()` 现场拼，只装口癖/情绪标签/说话习惯，
  不重复身份、不重复执行层说明。

拼接方式：**数据驱动 + 标记幂等去重**。`main.py::_prepare_chat()` 在往 system 消息尾部追加前，
先检查有没有 `她的说话习惯（从她真实发言里统计出来的）` 这个标记，有就不重复追加。
前端 `index.html` 也独立拼了一份（`personaPromptBlock()`），保证前端自己发请求时行为一致。

**开关**：前端 `personaOn`（默认开）。关掉就不注入人格片段。
人设页可以直接看人设原文、编辑 JSON、保存（保存前自动备份成 `.json.bak-时间戳`）。

### 1.2 安全层（`puppy_agent/core/safety.py`）

所有工具调用都要过这一关，顺序是：选工具 → 校验参数 → 判风险 → 要确认 → 执行 → 写审计。

| 机制 | 说明 |
|---|---|
| **默认拒绝** | 没在注册表里的工具一律拒绝，不存在"顺手调一下" |
| **三级风险** | `LOW` 直接执行 / `MEDIUM` 要确认 / `HIGH` 要确认 + 快照 |
| **确认令牌** | 中高风险必须带 `confirm_token`；令牌绑定**参数指纹**，换个参数就失效；TTL 300 秒；**一次性**，用过即废 |
| **参数校验** | 按 JSON-Schema 校验，参数不对直接拒，不会带着坏参数去执行 |
| **审计日志** | 每次调用都写 JSONL（工具名/参数/风险/成败/耗时），设置页能看 |
| **快照回滚** | HIGH 风险且带 `snapshot` 的工具，动手前先备份目标到 `.pet_trash` + `snapshots.json` 索引，出事能一键还原 |

**人格不参与风险判断。** 设计文档 §0 明确：人格层只管"怎么说"，安全层管"做不做"。
所以人格里写的"要谨慎"之类的话，**不会**变成"拒绝执行"。

### 1.3 工具契约（`puppy_agent/core/plugin.py`）

```
ToolSpec        一个工具：名字/描述/参数 schema/风险等级/是否破坏性/是否需快照/执行体
PluginManifest  一个插件：id/名字/版本/描述/最低核心版本
Plugin          插件基类：tools() 声明工具，setup(ctx) 拿上下文
RiskLevel       low / medium / high
```

插件通过 `RuntimeContext` 拿到：数据目录、安全门、人格、`config`（桌宠把真实记忆库路径塞在这儿）。

### 1.4 可插拔（热加载）

- 运行时扫 `plugins/*.py`，用 `importlib.util.spec_from_file_location` 动态导入；
- **同名工具直接报错**，不覆盖（避免两个插件抢一个工具名）；
- `min_core_version` 不满足就拒绝加载；
- 插件目录有两处：包内自带的 + **exe 旁边用户自己放的 `plugins\`**（放个 .py 进重启就生效）；
- 支持运行时 `load` / `unload` 单个插件，不影响其余。

### 1.5 exe 模式引导（`puppy_agent_boot.py`）

**为什么要这个文件**：PyInstaller 只认静态 import，而且我们**不想**让插件进 PYZ——
插件必须以**磁盘文件**形式存在才能热插拔。

做法：
1. 打包时 `--add-data "puppy_agent;puppy_agent"`，整个包原样拷进解包目录；
2. 运行时由 `puppy_agent_boot.boot()` 把 `puppy_agent` / `.core` / `.plugins` 三个**空壳包**
   挂到 `sys.modules` 上（`types.ModuleType` + `__path__`），让相对导入
   （`from ..core.plugin import ...`）能落到磁盘 .py 上；
3. 源码运行时走正常 import 路径，exe 运行时才用空壳包；
4. **引导失败不抛异常**——桌宠本体照常跑，只是 Agent 功能显示"不可用"。

### 1.6 HTTP 接口（`main.py`）

GET：
| 路径 | 作用 |
|---|---|
| `/api/agent/describe` | Agent 总览：核心版本、插件列表、工具清单（带风险等级） |
| `/api/agent/persona` | 人格画像（含现场拼的人设片段） |
| `/api/agent/persona/raw` | 人设原文 JSON（给人设页编辑用） |
| `/api/agent/audit?limit=` | 审计日志 |
| `/api/agent/snapshots` | 快照列表 |

POST：
| 路径 | 作用 |
|---|---|
| `/api/agent/call` | 调工具：`{tool, args, confirm_token}` |
| `/api/agent/restore` | 还原快照 |
| `/api/agent/reload` | 重新扫插件目录 |
| `/api/agent/persona/save` | 保存人设（写前自动备份） |

全部遵循统一约定：`{ok:true, ...}` / `{ok:false, message}`。

### 1.7 前端第 10 页「Agent」（`index.html`）

设置页导航新增第 10 页 🧩 Agent，包含：
- 运行时状态卡片（重新加载插件 / 刷新）
- 工具清单（每条带**风险徽标**）
- 人格画像展示
- 审计日志列表
- 快照列表 + 还原按钮

外加人设页的人格画像区块：查看、编辑 JSON、保存。

---

## 2. 验收结果

### 2.1 专项脚本

```
python tools\verify_agent_core.py     →  64 项通过，0 项未过     总判定：PASS
python tools\verify_agent_wiring.py   →  59 项通过，0 项未过     总判定：PASS
```

`verify_agent_core.py` 覆盖：契约、风险分级、默认拒绝、确认令牌一次性、
快照与还原、审计写入、人格渲染长度、**人格无权否决执行**。

`verify_agent_wiring.py` 覆盖 3 组：
1. **后端**：工具清单、风险等级齐全、**高风险工具都带快照或破坏性标记**、中高风险要确认、
   令牌一次性、假令牌被拒、快照/还原、人设片段内容与长度、**人设注入幂等**（连调两次只注入 1 次）；
2. **真实 HTTP**：起真的服务器，GET/POST 全部打一遍（describe/persona/audit/snapshots/
   persona-raw/call/reload），含"低风险直接执行""高风险先要确认""给了令牌能执行""假令牌被拒"
   "不存在的工具报错"；
3. **前端**：`index.html` 里 Agent 页 HTML、导航项、接线函数、工具清单渲染、风险徽标、
   审计列表、快照列表与人格画像展示、人设页查看/编辑/保存入口、prompt 拼了人设、人格异步拉取缓存。

### 2.2 全量自检

```
python tools\run_all_checks.py  →  共 12 项，通过 12 项，未过 0 项    总判定：PASS（退出码 0）
```

12 项 = 原来 10 项记忆相关 + 新增 2 项 Agent 验收（已并入 `tools\run_all_checks.py`）。

### 2.3 exe 模式验收（真实打包产物）

```
python tools\launch_and_verify.py  →  通过 24 项，未过 0 项    总判定：PASS
```

这个脚本干的事：**taskkill 旧实例 → 启动新 exe → 等端口 → 跑 exe 专项验收**，
一条龙（沙箱里每条命令结束都会清掉子进程，拆成两次跑必然失败）。

`tools\verify_exe_agent.py` 5 组：
1. **Agent 运行时**：核心版本、9 个工具、3 个插件（filesystem/memory/messenger）、三级风险齐全；
2. **插件是磁盘文件（热插拔）**：往 exe 旁边的 `plugins\` 丢一个探针 .py → 调 `/api/agent/reload`
   → 工具数从 9 涨到 10 → **能调用新插件的工具**（返回 pong）→ 删掉探针再重载。
   **这一条是"可插拔"最硬的证据：改插件 .py 重启就生效，不用重打包。**
3. **人格画像**：片段有内容、260 字不超上限、带口癖/情绪标签、原文能读、口癖与情绪标签字段都在；
4. **安全层在 exe 模式下照样拦得住**：高风险先要确认、令牌已发、假令牌被拒、不存在的工具被拒、
   低风险直接执行（且记忆库路径对得上——证明 config 传参在 exe 模式下也生效）；
5. **审计与快照**：审计有条目、快照接口可用。

### 2.4 真实数据

```
记忆库 episodes=13733   facts=0
  来源分布：wechat=13373（聊天记录）  desktop=360（日记/资料）
人设片段长度 260 字（上限 300）
exe 模式：核心 v1.0.0，插件 3 个，工具 9 个，热插拔后 10 个
```

**说明**：聊天记录已经进库了（13373 条来自 wechat），之前文档里"聊天记录还没进库"的说法已过时。
`facts`（结构化事实）还是 0 —— 事实要从对话里抽，聊起来之后 `memory_extract.py` 会自己攒。

---

## 3. 这一轮踩的坑（都是真踩过的）

1. **`msg_send` 是 HIGH 风险但既没快照也没破坏性标记** —— 验收卡住。
   外发消息出去就收不回来了，属于不可逆操作 → 补 `destructive=True`。
2. **记忆插件找不到记忆库** —— 它默认找 `agent_data\记忆\memory.db`，
   而真库在项目根的 `记忆\memory.db`。
   改法：插件 `memory.py::_db()` 优先读 `ctx.config["memory_db"]`，
   再由 `main.py::_agent_build()` 把真实路径塞进 `config` 传进去。**两处都要改，只改一处没用。**
3. **`puppy_agent/__init__.py` 报 SyntaxWarning** —— docstring 里有 `plugins\ 里` 这种反斜杠，
   Python 3.13 会警告。加 `r"""` 前缀解决。
4. **测试断言写错** —— `len(rl.get("tools"))` 报 `TypeError: object of type 'int' has no len()`，
   因为 `/api/agent/reload` 返回的 `tools` 是**工具个数**（整数）不是列表。改断言为整数比较。
5. **本机有 HTTP 代理环境变量**（`HTTP_PROXY=http://127.0.0.1:56033`），
   `urllib` 会把 `127.0.0.1` 的请求也发给代理 → **502 Bad Gateway**，
   看起来像"桌宠没起来"，其实是代理捣乱。
   解法：`urllib.request.build_opener(urllib.request.ProxyHandler({}))` 显式绕过代理。
   `tools\launch_detached.py`、`tools\swap_exe.py` 都踩过这个坑，已统一修。
6. **exe 旁边的 `plugins\` 目录不会自动建** —— `_agent_user_plugin_dir()` 原来只返回路径不建目录，
   导致热插拔时写文件报 `No such file or directory`。改成 `mkdir(parents=True, exist_ok=True)`，
   建不出来也不抛异常（不让桌宠因此挂掉）。
7. **探针插件自己写错了字段名** —— 我写 `ToolSpec(params={})`，实际字段叫 `parameters`，
   运行时日志报 `ToolSpec.__init__() got an unexpected keyword argument 'params'`。
   **教训**：插件契约字段名要照 `puppy_agent/core/plugin.py` 抄，别凭印象写。
8. **沙箱里进程会被连带清掉** —— 用 `Start-Process` 启动的 exe，PowerShell 调用一结束就没了，
   端口也就没了。必须"启动 + 验收"放在**同一次调用**里，
   所以有了 `tools\launch_and_verify.py` 这条一条龙脚本。

---

## 4. 改动文件与回滚方式

### 4.1 新增文件
| 文件 | 说明 | 回滚 |
|---|---|---|
| `puppy_agent_boot.py` | exe 模式引导（空壳包方案） | 直接删 |
| `puppy_agent\__init__.py` | 补包声明（原缺失） | 直接删 |
| `puppy_agent\core\plugin.py` | 工具契约 `ToolSpec`/`PluginManifest`/`RiskLevel` | 删整个 `puppy_agent\` |
| `puppy_agent\core\safety.py` | 安全层（分级/令牌/审计/快照） | 同上 |
| `puppy_agent\core\runtime.py` | 运行时（发现/加载/调度） | 同上 |
| `puppy_agent\core\personality.py` | 人格层（加载/渲染/片段/保存） | 同上 |
| `puppy_agent\plugins\filesystem.py` | 文件工具（读写/删除） | 同上 |
| `puppy_agent\plugins\memory.py` | 记忆库工具（检索/统计/软删） | 同上 |
| `puppy_agent\plugins\messenger.py` | 消息代发工具 | 同上 |
| `tools\verify_agent_core.py` | 核心验收 | 直接删 |
| `tools\verify_agent_wiring.py` | 接线验收 | 直接删 |
| `tools\verify_exe_agent.py` | exe 模式专项验收（24 项） | 直接删 |
| `tools\launch_and_verify.py` | 启动 + 验收一条龙（沙箱里必须同一次调用） | 直接删 |
| `tools\_diag_agent.py` | 临时诊断：打印运行时加载日志 | 直接删 |

### 4.2 修改文件
| 文件 | 改了什么 | 怎么回退 |
|---|---|---|
| `main.py` | 顶部 `import puppy_agent_boot`；新增 Agent 段（约 2130–2400 行）：`_agent_build` / `agent_runtime` / `_agent_describe` / `_agent_persona` / `_persona_data_block` / `_agent_call` / `_agent_audit` / `_agent_snapshots` / `_agent_restore` / `_agent_save_persona` / `_agent_reload`；GET 加 5 个 `/api/agent/*`；POST 加 4 个；`_prepare_chat()` 尾部加人格注入 | 从 `文档\backup\main.py.bak-20261027-p5` 恢复（注意：p5 版是**本轮改动前**的备份，不含 Agent 段） |
| `index.html` | 新增 `AGENT_PAGE_HTML`；`agApi`/`agRefresh`/`agAuditRefresh`/`agSnapRefresh`/`bindAgentPage`；`SET_PAGES` 加 `{id:"agent"}`；`buildSettingsShell()` 加分支；新增 `personaBlockText`/`personaPromptBlock`/`loadPersonaBlock`；`systemPrompt()` 拼人设；人设页加人设画像区块 | 从 `文档\backup\index.html.bak-20261027-p5` 恢复 |
| `puppy_agent\core\runtime.py` | `__init__` 增 `extra_dirs`/`config`；`discover()` 多目录扫描 + 文件名去重 | 从 `文档\backup\puppy_agent_core_runtime.py.bak-20261027` 恢复 |
| `puppy_agent\core\personality.py` | `persona_prompt()` 增 `include_identity`；新增 `persona_data_block()`、`save_persona()` | 同上 |
| `puppy_agent\plugins\messenger.py` | `msg_send` 加 `destructive=True` | 同上 |
| `puppy_agent\plugins\memory.py` | `_db()` 优先读 `ctx.config["memory_db"]` | 同上 |
| `tools\run_all_checks.py` | 加 2 项 Agent 验收；标题从"记忆功能全量自检"改成"全量自检" | 从 `文档\backup\tools__run_all_checks.py.bak-20261027` 恢复 |
| `tools\rebuild_exe.py` | 加 `--add-data "persona.json;."`、`--add-data "puppy_agent;puppy_agent"`、`--hidden-import puppy_agent_boot` | 从 `文档\backup\tools__rebuild_exe.py.bak-20261027` 恢复 |
| `tools\swap_exe.py` | 探测里加 Agent 接口检查 | 从 `文档\backup\tools__swap_exe.py.bak-20261027` 恢复（若无则从 git 恢复） |
| `tools\launch_detached.py` | 绕过本机 HTTP 代理，否则探测 502 | 从 `文档\backup\tools__launch_detached.py.bak-20261027` 恢复（若无则从 git 恢复） |

### 4.3 备份位置
```
文档\backup\
  main.py.bak-20261027-p5              （本轮改 Agent 前）
  index.html.bak-20261027-p5           （本轮改 Agent 前）
  tools__run_all_checks.py.bak-20261027
  tools__rebuild_exe.py.bak-20261027
  puppy_agent_boot.py.bak-20261027
  STATUS.md.bak-20261027
```

### 4.4 最小回退（只关掉 Agent，不动别的）
`index.html` 的 `SET_PAGES` 里删掉 `{id:"agent", ...}` 那行 → 第 10 页消失；
后端的 `/api/agent/*` 留着也不影响任何现有功能（没人调）。
**这一条不用重打包 exe**，index.html 是热生效的。

---

## 5. 还没做的（下一阶段）

- **`facts` 还是 0**：事实要从对话里抽，聊起来之后 `memory_extract.py` 会自己攒。
  （聊天记录本身已进库：wechat 13373 条 + 日记 360 条，共 13733 条。）
- **插件市场 / 一键安装**：现在只能手动丢 .py 进 `plugins\`，还没有图形化的插件管理。
- **工具自主调用循环**：现在是"人点一下调一个工具"，还没做成"她自己判断该用哪个工具"。
- **多用户 / 权限隔离**：现在是单用户本机设计。
