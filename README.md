# 礼貌小狗 · 千逐

<p>
  <img alt="version" src="https://img.shields.io/badge/version-1.1-2EA043?style=flat-square">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10-3776AB?style=flat-square">
  <img alt="runtime" src="https://img.shields.io/badge/runtime-pywebview%20%2B%20WebView2-5C2D91?style=flat-square">
  <img alt="llm" src="https://img.shields.io/badge/LLM-local%20brain%20%7C%20Ollama-000000?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/privacy-local--first-2EA043?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-3DA639?style=flat-square">
</p>

> 一只安静蹲在 Windows 桌面角落的电子小狗。

平时它就以透明立绘的模样贴在屏幕边上，不弹窗、不抢戏；想找人搭句话时，点它一下，会弹出一个像微信一样的小窗口。它说话偏短、记性不错，会把回复拆成几条气泡慢慢发过来，偶尔丢个表情包，兴致来了还能现场画张图，或者开麦给你唱首歌。

做它的初衷很简单：想要一个**真正待在自己电脑里的桌面伙伴**。所以它默认全程不出网——不挂云端 API、断着网也能靠内置本地大脑或本地 Ollama 正常聊天，你记下的备忘、提醒和聊过的日常都只留在本地目录里。

## 它能陪你做什么

| 能力 | 具体体验 |
| :--- | :--- |
| **透明立绘桌宠** | WebView2 逐像素透明窗口，无边框可随意拖拽；单击开合聊天窗，右键呼出功能菜单。 |
| **本地优先大脑** | 不接大模型也能靠 `brain.json` 语料 + 中文二元组打分聊天；接入本地 `127.0.0.1:11434` 的 Ollama 后切换真实大模型。 |
| **自然聊天节奏** | 按 `\|\|` 拆条逐条弹出并显示「对方正在输入…」；内置 20 张表情包（`[STICKER:名字]`），支持自绘图像（`[DRAW:english prompt]`）和动态快捷回复（`[QUICK:a,b]`）。 |
| **本地记忆检索** | 基于 SQLite + FTS5 trigram 与二元组打分检索记忆库，取高分条与近期记录注入上下文，支持软删、墓碑与快照备份。 |
| **日常小助手** | 内置 8 个工具（`time_now`、`calc`、`note_add`、`note_list`、`remind_*`、`app_open`），支持沙盒内文件读写，越界需确认，敏感路径硬拒绝。 |
| **唱歌台与音色克隆** | 独立 `puppy-sing` 模块支持谐波合成演唱与听原唱扒歌自学；可选接 F5-TTS / GPT-SoVITS 本地服务，并带音色身份证防跑偏。 |
| **纯 Web 服务器模式** | 同一份 `main.py` 加 `--server` 即可跳过桌面 GUI 仅跑 HTTP 服务，方便交给 IIS / Nginx / 宝塔反代。 |

## 1.1 更新了什么

相比 1.0，这一版把「Agent」做成了结构化架构，也补齐了发布工程：

1. **Agent 2.0（规划 / 执行分离）**：规划器先出计划，执行器逐步运行；规则命中直接走本地逻辑，未命中再调用模型。
2. **执行器预算与防循环**：默认最多 3 步 / 20 秒，超时即停；同一步骤重复出现直接拒绝。
3. **风险分级 + 确认令牌**：`LOW` 级操作直接执行；`MEDIUM / HIGH` 级先签发一次性令牌，等你确认后再带令牌续跑。
4. **插件 SDK v2 + 信任清单**：插件声明 `permissions`；包内自带默认信任，用户自放插件默认禁用，文件一旦被改动自动卸载。
5. **记忆库重构**：迁移至 SQLite + FTS5 trigram，支持软删、墓碑与快照备份，删掉的内容不会“复活”。
6. **发布工程补齐**：加入 `pack_check` 自检、`package_release` 白名单组装、语料脱敏与 204 条黄金集评测。
7. **服务器模式回归**：仓库源码支持 `python main.py --server` 纯 HTTP 运行。

## 快速开始

### 方式一：直接用发布包（推荐）

下载仓库根目录的 `小狗桌宠1.1.zip`，解压后双击运行：

```text
01_程序\PolitePuppy.exe
```

无需安装 Python，开箱即用，全局单实例运行。

### 方式二：从源码运行

运行前请确保系统已安装 **WebView2 Runtime**（Windows 11 默认自带）。

1. 启动本地 Ollama 并拉取模型（若仅使用内置本地大脑可跳过）：

```powershell
ollama serve
ollama pull qwen2.5:7b
```

2. 创建 Python 3.10 虚拟环境并安装依赖：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

3. 启动桌宠（或直接双击 `run.bat`）：

```powershell
.\.venv\Scripts\python.exe main.py
```

> 如需使用唱歌台与音色克隆，额外安装 `requirements-voice.txt`。

## 交互与快捷键

### 鼠标操作

- **单击立绘**：展开 / 收起聊天窗口
- **左键拖动**：自由调整小狗在桌面上的位置
- **右键立绘或托盘图标**：打开聊天、唱歌台、扒歌学习、设置，以及隐藏、置顶、缩放、开机自启与退出

### 全局热键

| 快捷键 | 功能 |
| :--- | :--- |
| `Ctrl+Alt+V` | 朗读上一条回复 |
| `Ctrl+Alt+Space` | 暂停 / 继续朗读 |
| `Ctrl+Alt+X` | 停止朗读 |
| `Ctrl+Alt+I` | 麦克风听写 |
| `Ctrl+Alt+S` | 打开设置 |
| `Ctrl+Alt+G` | 打开唱歌台（小狗唱歌） |
| `Ctrl+Alt+L` | 扒歌学习（听原唱自学） |

## 数据与隐私

- **默认目录**：个人数据保存在 `%LOCALAPPDATA%\PolitePuppyPet\data\`（含 `记忆\memory.db`、`memory_diary.txt`、`agent_data\`）；程序同级目录保存 `voices\` 与 `.voice_cache\`。
- **便携模式**：设置环境变量 `POLITE_PUPPY_DATA` 即可自定义数据目录。
- **网络边界**：内置本地大脑与本地 Ollama 模式全程不上传数据；若手动切换至云端 API，会弹出知情提示，且默认不附带记忆检索片段与日记（当前聊天窗已有的历史消息仍会随请求发送）。
- **仓库脱敏**：`.gitignore` 已默认排除 `brain.json`、`voices\`、`记忆\` 等个人数据目录。

## 架构、插件与服务器模式

- **同源本地服务**：`main.py` 监听 `127.0.0.1`，单文件前端 `index.html`（无构建链）通过同源 `/api/*` 与 SSE 通信，并对 `Host`、`Origin`、`Sec-Fetch-Site` 做三重校验。
- **Agent 2.0 链路**：规则规划器与模型规划器（严格 JSON + schema 校验）协同，内建预算控制、防循环、敏感路径（`.ssh`、`.env`、记忆库本身）硬拒绝、确认令牌续跑、污点传导降级及观察压缩。
- **自定义插件**：可按 SDK v2 在 `plugins\` 目录编写扩展，默认禁用，需在设置中手动开启。
- **服务器模式**：源码支持通过 `python main.py --server` 启动纯 Web 服务，可配置 `POLITE_PUPPY_SERVER`、`POLITE_PUPPY_PORT`（默认 `8765-8795`）、`POLITE_PUPPY_BIND`、`POLITE_PUPPY_ALLOWED_HOSTS`。（注：`小狗桌宠1.1.zip` 内构建于 `2026-11-21` 的 exe 暂不含 `--server` 参数。）

## 测试、打包与已知限制

```powershell
# 冒烟测试（共 230 条；缺少个人语料/人设/交付布局时会自动跳过 22 条）
python tests\run_tests.py

# Agent 黄金集评测（204 条）
python scripts\agent_eval.py

# 重新打包 exe
.\scripts\build_exe.ps1
```

**已知限制**

- 全局单实例运行；内置本地大脑的静态语料规模有限。
- 记忆检索 `FTS_MIN_LEN = 3`，1–2 字查询回退 `LIKE` 速度偏慢。
- 语音输入依赖 Web Speech 或系统 SAPI；唱歌台与音色克隆需安装 `requirements-voice.txt`。
- 源码模式目前基于 Python 3.10 验证，暂未验证 Python 3.11+。

## 许可

[MIT License](LICENSE) © 2026 oTATo666
