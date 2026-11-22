# 礼貌小狗 · 千逐

> 一只住在 Windows 桌面上的电子小狗：透明立绘 + 仿微信聊天窗，说话短、记性好，**默认全程不出网**。

<p>
  <img alt="version" src="https://img.shields.io/badge/version-1.1-2EA043?style=flat-square">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10-3776AB?style=flat-square">
  <img alt="runtime" src="https://img.shields.io/badge/runtime-pywebview%20%2B%20WebView2-5C2D91?style=flat-square">
  <img alt="llm" src="https://img.shields.io/badge/LLM-local%20brain%20%7C%20Ollama-000000?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/privacy-local--first-2EA043?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-3DA639?style=flat-square">
</p>

她不是「一问一答」的客服。她会连发三条、会丢表情包、会把你说过的事记很久；你让她动手，她也会先想一遍再动手——查文件、算数、记笔记、设提醒，动之前还要你点头。

---

## 1.1 更新了什么

相比 1.0，这一版主要是把「Agent」这条线做成了结构化的东西，顺手补齐了发布工程。

| # | 变化 | 说明 |
|---|---|---|
| 1 | **Agent 2.0：规划 / 执行分离** | 先由规划器出一份计划，再交给执行器逐步跑。规则能命中就不叫模型（省时省钱）；命中不了才问模型 |
| 2 | **执行器有预算** | 默认最多 **3 步 / 20 秒**，超了即停；同一步骤重复出现直接拒（防循环） |
| 3 | **风险分级 + 确认令牌** | `LOW` 直接跑，`MEDIUM`/`HIGH` 先给一次性令牌，你确认后带令牌才执行 |
| 4 | **插件 SDK v2 + 信任清单** | 插件可声明 `permissions`；包内自带默认信任，用户自己放的默认禁用，文件一改就自动卸下 |
| 5 | **记忆库重构** | SQLite + FTS5 trigram；支持软删 / 墓碑 / 快照备份，删掉的事不会被「复活」 |
| 6 | **发布工程** | `pack_check` 自检、`package_release` 白名单组装、语料脱敏、204 条黄金集评测 |
| 7 | **服务器模式**（仓库版补回） | `python main.py --server` 只跑 HTTP 服务，交给 IIS / Nginx / 宝塔反代 |

---

## 核心特性

| 能力 | 实现要点 |
|---|---|
| **透明立绘** | WebView2 逐像素透明窗口，无边框可拖拽；单击开合聊天、右键出功能菜单 |
| **本地大脑** | 不接模型也能聊：`brain.json` 语料 + 中文二元组打分；接上 `127.0.0.1:11434` 的 Ollama 则走真模型 |
| **多气泡节奏** | 模型输出按 `\|\|` 拆条，逐条弹出，期间显示「对方正在输入…」 |
| **表情包** | 20 张内置表情，`[STICKER:名字]` 渲染成图片气泡；名字对不上自动降级成文字 |
| **自绘图像** | `[DRAW:english prompt]` 触发，先出骨架屏再换图 |
| **动态快捷回复** | `[QUICK:a,b]` 实时改写底部标签条，点一下即发送 |
| **记忆 RAG** | 请求前按二元组打分检索记忆库，取高分若干条 + 最近几条注入 System Prompt |
| **日常助手** | 8 个内置工具：`time_now` / `calc` / `note_add` / `note_list` / `remind_*` / `app_open` |
| **文件与记忆工具** | 沙盒内读写、越界要确认；敏感路径（`.ssh`、`.env`、记忆库本身）一律硬拒 |
| **唱歌台** | 独立 `puppy-sing` 模块：谐波合成演唱 + 扒歌自学（音高提取 / 人声分离 / ASR） |
| **音色克隆** | 可选接 F5-TTS / GPT-SoVITS 本地服务，桌宠启动时自动拉起；带音色身份证防跑偏 |
| **服务器模式** | 同一份 `main.py` 加 `--server` 即可上服务器，桌宠 GUI 部分自动跳过 |

---

## 快速开始

### 方式一：直接用发布包（推荐）

下载根目录的 **`小狗桌宠1.1.zip`**，解压后双击 `01_程序\PolitePuppy.exe`。不用 Python、不用安装。

> 程序是**全局单实例**的。旧版还在跑时双击新版，新进程会把旧实例唤回前台再自己退出，并弹框告诉你「正在运行：x / 你刚双击：y」。换版本请先到托盘右键 → 退出。

### 方式二：源码运行

```powershell
# 1)（可选）准备本地模型
ollama serve
ollama pull qwen2.5:7b

# 2) 装依赖（Python 3.10.x；3.11+ 的 pythonnet 在 Windows 上不稳）
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) 启动
.\.venv\Scripts\python.exe main.py
```

也可以直接双击 `run.bat`（优先用 `.venv` 里的解释器）。

> 依赖 WebView2 Runtime。Win10/11 通常自带；缺了会白屏，去[微软官网](https://developer.microsoft.com/microsoft-edge/webview2/)装一次即可。

---

## 交互与快捷键

| 操作 | 效果 |
|---|---|
| 单击立绘 | 打开 / 收起聊天窗 |
| 按住左键拖动 | 移动桌宠 |
| 右键立绘 / 托盘 | 聊天、唱首歌、扒歌学习、设置、隐藏、置顶、大小、开机自启、退出 |
| `Ctrl+Alt+V` | 朗读上一条回复 |
| `Ctrl+Alt+Space` | 暂停 / 继续朗读 |
| `Ctrl+Alt+X` | 停止朗读 |
| `Ctrl+Alt+I` | 麦克风听写 |
| `Ctrl+Alt+S` | 打开设置 |
| `Ctrl+Alt+G` | 小狗唱歌（唱歌台） |
| `Ctrl+Alt+L` | 扒歌学习（听原唱自学） |

后四个（朗读 / 暂停 / 停止 / 麦克风）都能在「设置 → 键位」里改或留空；对话窗口本身不占全局热键，避免和输入法、截图工具抢键位。

---

## 数据与隐私

**记忆、日记、Agent 审计**都不在程序目录：

| 路径 | 内容 |
|---|---|
| `%LOCALAPPDATA%\PolitePuppyPet\data\记忆\memory.db` | 记忆库（聊天记录、事实、关系），SQLite + FTS5 trigram |
| `%LOCALAPPDATA%\PolitePuppyPet\data\memory_diary.txt` | 日记，一行一件事，记事本可直接改 |
| `%LOCALAPPDATA%\PolitePuppyPet\data\agent_data\` | Agent 审计日志、快照回收站、沙盒工作区 |
| `voices\` | 音色档案与克隆样本（跟程序放一起） |
| `.voice_cache\` | 语音缓存，删了自动重建 |

设环境变量 `POLITE_PUPPY_DATA=D:\somewhere` 可整体挪走（便携模式）。

**什么会离开这台电脑：**

| 模式 | 会不会外发 | 发什么 |
|---|---|---|
| 本地大脑 / 演示 | 不会 | — |
| 本地（Ollama `127.0.0.1`） | 不会 | — |
| 云端 API | **会** | 你说的话、她的回复、系统提示词；发图时附带的图片描述 |

切云端会弹一次知情提示；云端模式默认**不带**记忆检索片段和日记，想带得自己关掉开关。但聊天窗里已有的历史消息会随这一轮请求发出去——介意就先清空。

**这个仓库不带任何个人数据**：`brain.json`（语料）、`voices\`（音色样本）、`记忆\`（聊天库）都在 `.gitignore` 里。想要自己的语料，用 `tools/build_brain.py` 从你自己的文本生成。

---

## 架构

```
┌──────────────── pywebview / WebView2 ────────────────┐
│  index.html   聊天 UI · 透明立绘 · 表情 · 生图 · 设置 │
│               单文件，无构建链，全部前端逻辑           │
└──────────────────────────┬───────────────────────────┘
                           │ 同源请求 http://127.0.0.1:<port>
┌──────────────────────────▼───────────────────────────┐
│  main.py   本地 HTTP 服务 · 托盘 · 热键 · 语音 · 代理 │
└────────┬──────────────────────────────┬──────────────┘
         │ /v1/chat/completions (SSE)   │ 子进程 / HTTP
┌────────▼─────────┐          ┌─────────▼───────────────┐
│ Ollama :11434    │          │ F5-TTS / GPT-SoVITS     │
│ 或本地 brain.json │          │ puppy-sing 唱歌台        │
└──────────────────┘          └─────────────────────────┘
                                 ▲
┌────────────────────────────────┴─────────────────────┐
│ puppy_agent/  规划器 · 执行器 · 安全层 · 插件运行时   │
└──────────────────────────────────────────────────────┘
```

**为什么不会跨域**：`main.py` 起一个只监听 `127.0.0.1` 的 `http.server`，把页面、素材与 `/api/*` 一起发出；pywebview 加载的是 `http://127.0.0.1:端口/`，所以 `fetch('/api/chat')` 属同源。SSE 用 chunked 原样透传，前端逐字接收。

**安全**：`Host` / `Origin` / `Sec-Fetch-Site` 三重校验，跨站与 DNS rebinding 一律 403；`.env`、`main.py`、`记忆/memory.db` 等敏感路径全部 404；`/api/*` 还要带 `X-Puppy-Token`——每进程随机一把，只经 pywebview 的桥注入前端，**不写进 HTML**。

> 地址请写 `127.0.0.1` 而不是 `localhost`：WebView2 有时解析到 `::1`，而 Ollama 只监听 IPv4。

---

## Agent 2.0

一次请求的路径：

```
用户这句话
   │
   ├─ 规则规划器（快、免费）── 命中 → 直接出计划
   │
   └─ 模型规划器（可选）──── 严格 JSON + schema 校验
                              ├ 不像请求 → 不调模型，当闲聊
                              ├ 注入 / 越权输出 → 整份计划丢弃
                              └ 出错逐级回落，不把异常抛给用户
   │
   ▼
执行器：逐步跑（默认 3 步 / 20 秒）
   ├ 同步骤第二次出现 → 拒（防循环）
   ├ 硬拒（敏感路径）→ 取消剩余步骤
   ├ MEDIUM/HIGH → 挂起 + 一次性确认令牌 → /api/agent/resume 续跑
   ├ 污点传导：本步用了不可信内容，后续写操作降级
   └ 观察压缩：文件原文不进提示词链
```

设置页的「Agent」页可以选规划模式（`off` / `rule` / `local` / `cloud`）、看每日上限、管插件信任列表、看提醒清单和最近审计。

**自己写插件**：放进 `plugins\`（exe 旁边，源码运行时就是项目根下），继承 `Plugin`，用 `ToolSpec` 声明 `name` / `risk` / `permissions` / `timeout_s`。用户插件默认禁用，需要在设置页手动启用（启用时绑定文件哈希）。

```python
from puppy_agent.core.plugin import Plugin, PluginManifest, RiskLevel, ToolSpec


class HelloPlugin(Plugin):
    manifest = PluginManifest(
        id="hello",
        name="打个招呼",
        version="1.0.0",
        description="最小示例",
    )

    def tools(self):
        return [ToolSpec(
            name="hello",
            description="打个招呼",
            risk=RiskLevel.LOW,
            invoke=lambda args: {"ok": True, "message": "汪"},
        )]
```

---

## 服务器模式（可选）

同一份 `main.py`，加个参数就变成纯 Web 服务——不开窗口、不建托盘、不注册热键：

```powershell
$env:POLITE_PUPPY_SERVER = "1"
$env:POLITE_PUPPY_PORT = "8765"
$env:POLITE_PUPPY_BIND = "127.0.0.1"          # 交给反代，就别直接对公网
$env:POLITE_PUPPY_ALLOWED_HOSTS = "dog.example.com"
python main.py --server
# → SERVER_READY http://127.0.0.1:8765
```

| 变量 | 作用 |
|---|---|
| `POLITE_PUPPY_SERVER` | `1` / `true` / `yes` / `on` 开启服务器模式（等价 `--server`） |
| `POLITE_PUPPY_PORT` | 固定端口；不填则在 8765–8795 里挑第一个空闲的 |
| `POLITE_PUPPY_BIND` | 监听地址，默认 `127.0.0.1` |
| `POLITE_PUPPY_ALLOWED_HOSTS` | 额外放行的 Host / Origin（反代到域名必填，否则 403） |

反代、HTTPS、备份与排查见 [`部署指南.md`](部署指南.md) 和 [`server/`](server/)。

---

## 目录结构

```
礼貌小狗桌宠/
├─ main.py                  # 窗口 / 托盘 / 热键 / 本地 HTTP / 语音 / 服务器模式
├─ index.html               # 全部前端逻辑（单文件，无构建链）
├─ puppy_core.py            # 会话与记忆核心
├─ local_brain.py           # 本地大脑（brain.json + 打分选句）
├─ identity.py              # 身份名统一入口（设置 > 人设 > 中性默认）
├─ memory_organize.py       # 记忆离线整理
├─ puppy_agent/             # Agent 2.0：core/ 运行时 · plugins/ 内置插件
├─ puppy-sing/              # 唱歌台：演唱合成 / 扒歌自学
├─ tools/                   # 运行期才 import 的模块（音色、记忆抽取、F5 网关）
├─ scripts/                 # 打包 / 自检 / 脱敏 / 评测（不参与运行）
├─ tests/                   # 230 条用例（python tests\run_tests.py）
├─ server/                  # 服务器部署脚本（宝塔 / IIS 反代）
├─ docs/                    # 架构、插件 SDK、UI 契约、后续规划
├─ 表情包/                   # 20 张内置表情
├─ 小狗桌宠1.1.zip           # 打包好的可执行发布包
└─ 使用说明.txt              # 面向使用者的操作说明
```

---

## 测试与打包

```powershell
# 冒烟测试：230 条，起临时端口，只写临时目录
python tests\run_tests.py

# Agent 黄金集：204 条（读 / 密钥文件 / 列目录 / 统计 / 搜索 / 闲聊边界 / 注入 / 越权 / 多步）
python scripts\agent_eval.py

# 打包自检（交付包内跑，可连带校验 exe）
python scripts\pack_check.py --exe .\PolitePuppy.exe

# 按白名单组装外发包（自动排除 记忆/、voices/、.env，并扫用户名）
python scripts\package_release.py --with-exe --out ..\发布_v1
```

仓库是扁平布局，所以有 22 条用例会自动跳过——它们需要 `brain.json`（个人语料）或 `release/persona.json`（外发版人设）或 `01_程序/` 交付包布局，这三样都刻意不进公开仓库。跳过不是失败，`run_tests.py` 会照常返回 0。

重新打 exe（需要 Python 3.10 + PyInstaller）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

---

## 已知限制

- 同一时间只能跑一只（全局单实例）；
- 本地大脑语料有限，复杂问题答不好，接外部模型明显更好；
- 记忆检索 `FTS_MIN_LEN = 3`，1–2 字查询走 `LIKE '%词%'`，库很大时会变慢；
- 语音输入依赖 Web Speech 或系统 SAPI，两者都没有时会提示「打字吧」；
- 唱歌台与音色克隆需要额外的模型/依赖（`requirements-voice.txt`），不是开箱即用；
- 源码模式未验证 Python 3.11+（pythonnet 在 Windows 上的兼容问题）。

更细的待办见 [`docs/后续开发建议.md`](docs/后续开发建议.md)。

---

## 许可

[MIT](LICENSE) © 2026 oTATo666
