# 礼貌小狗 · 千逐

> 一个跑在 Windows 桌面上的本地 AI 伴侣：透明无边框立绘 + 仿微信聊天窗，大脑是本机 Ollama，**全程不出网**。

<p>
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square">
  <img alt="runtime" src="https://img.shields.io/badge/runtime-pywebview%20%2B%20WebView2-5C2D91?style=flat-square">
  <img alt="llm" src="https://img.shields.io/badge/LLM-Ollama%20local-000000?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/privacy-local--first-2EA043?style=flat-square">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-3DA639?style=flat-square">
</p>

她不是「一问一答」的客服式助手。她会连发三条、会丢表情包、会自己画图，也会在你三分钟没说话时先开口。

---

## 核心特性

| 能力 | 实现要点 |
|---|---|
| **透明立绘** | WebView2 逐像素透明窗口，无边框可拖拽；单击开合聊天、右键出功能菜单 |
| **本地大脑** | 走 `127.0.0.1:11434` 的 Ollama，对话、记忆、人设全部留在本机 |
| **多气泡节奏** | 模型输出按 `\|\|` 拆条，0.5–1.3 s 逐条弹出，期间显示「对方正在输入…」 |
| **表情包** | IndexedDB 存图，`[STICKER:名字]` 渲染为图片气泡，名字不匹配自动降级为文字 |
| **自绘图像** | `[DRAW:english prompt]` 触发，先出骨架屏再换图 |
| **动态快捷回复** | `[QUICK:a,b]` 实时改写底部标签条，点一下即发送 |
| **记忆 RAG** | 请求前按中文二元组打分检索记忆库，取高分 3 条 + 最近 2 条注入 System Prompt |
| **冷场唤醒** | 静默 3 分钟自动注入隐藏 Prompt，主动开腔（窗口收起时限流以省显存） |
| **三级语音输入** | Web Speech → Windows SAPI 听写 → 明确降级提示，避免「按钮在但永远没结果」 |
| **唱歌台** | 独立 `puppy-sing` 模块：谐波合成演唱、扒歌自学（音高提取 + 人声分离 + ASR） |
| **音色克隆** | 可选接入 F5-TTS / GPT-SoVITS 本地服务，桌宠启动时自动拉起 |

---

## 快速开始

### 方式一：直接用发布包（推荐）

下载根目录的 **`小狗桌宠1.0.zip`**，解压后双击 `PolitePuppy.exe`。无需 Python 环境。

### 方式二：源码运行

```powershell
# 1) 准备大脑
ollama serve
ollama pull qwen2.5:7b

# 2) 装依赖（Python 3.10+）
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3) 启动
.\.venv\Scripts\python.exe main.py
```

也可以直接双击 `run.bat`（自动优先使用 `.venv` 内的解释器）。

> 依赖 WebView2 Runtime。Win10/11 通常自带；缺了会白屏，去[微软官网](https://developer.microsoft.com/microsoft-edge/webview2/)装一次即可。

---

## 交互与快捷键

| 操作 | 效果 |
|---|---|
| 单击立绘 | 打开 / 收起聊天窗 |
| 按住左键拖动 | 移动桌宠 |
| 右键立绘 / 托盘 | 聊天、唱首歌、扒歌学习、设置、隐藏、置顶、大小、开机自启、退出 |
| `Ctrl+Alt+S` | 打开设置 |
| `Ctrl+Alt+G` | 小狗唱歌（唱歌台） |
| `Ctrl+Alt+L` | 扒歌学习（自动弹出扒歌窗口） |

对话本身不占全局快捷键，避免和其他软件抢键位；语音输出 / 暂停 / 停止 / 麦克风四个热键可在「设置 → 键位」自定义或留空。

---

## 架构

```
┌──────────────── pywebview / WebView2 ────────────────┐
│  index.html   聊天 UI · 透明立绘 · 表情 · 生图 · 设置 │
└──────────────────────────┬───────────────────────────┘
                           │ 同源请求 http://127.0.0.1:<port>
┌──────────────────────────▼───────────────────────────┐
│  main.py   本地 HTTP 服务 · 反向代理 · 托盘 · 热键    │
└────────┬────────────────────────────────┬────────────┘
         │ /v1/chat/completions (SSE)     │ 子进程
┌────────▼─────────┐            ┌─────────▼──────────────┐
│ Ollama :11434    │            │ F5-TTS / GPT-SoVITS    │
│ 本地推理          │            │ puppy-sing 唱歌台      │
└──────────────────┘            └────────────────────────┘
```

**为什么不会跨域**：`main.py` 起一个只监听 `127.0.0.1` 的 `http.server`，把页面、素材与 `/api/*` 一起发出；pywebview 加载的是 `http://127.0.0.1:端口/index.html` 而非 `file://`，因此 `fetch('/api/chat')` 属同源请求，浏览器不发预检。SSE 以 chunked 原样透传，前端可逐字接收；真落到 `file://` 场景时自动改调 `pywebview.api.chat()` 走 Python 内部通道。

> 地址请写 `127.0.0.1` 而不是 `localhost`：WebView2 有时把 `localhost` 解析到 `::1`，而 Ollama 只监听 IPv4。

---

## 目录结构

```
礼貌小狗桌宠/
├─ main.py                  # 窗口 / 托盘 / 热键 / 本地 HTTP / 代理 / 语音
├─ index.html               # 全部前端逻辑（单文件，无构建链）
├─ puppy_core.py            # 会话与记忆核心
├─ local_brain.py           # 本地推理封装
├─ puppy_agent_boot.py      # Agent 启动引导
├─ persona.json             # 人设
├─ PuppyAgent.config.json   # 模型与安全策略（默认指向本地 Ollama）
├─ dog_girl.png / .ico      # 立绘与图标（同名替换即可换形象）
├─ puppy-sing/              # 唱歌台：演唱合成 / 扒歌自学
├─ tools/                   # 运行时伴随脚本（TTS 网关、克隆服务、构建）
├─ 小狗桌宠1.0.zip          # 打包好的可执行发布包
└─ 使用说明.txt             # 面向使用者的操作说明
```

运行时数据落在 `%APPDATA%\PolitePuppyPet\`（`pet.log`、`settings.json`、`webview2\`）。设置环境变量 `POLITE_PUPPY_DATA=D:\somewhere` 可切为便携模式。

---

## 隐私设计

- 仓库**不包含**任何个人日记、记忆库或 `.env` 凭据；记忆与大脑为本地生成，用 `tools/build_brain.py` 从你自己的语料构建。
- 对话只发往 `127.0.0.1`；生图若启用则为可选的第三方接口，可不开。
- 发布包内不含账号信息，换机后首次启动即为干净状态。

---

## 构建 exe

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

或手动（关键参数：`--onefile --windowed`、`--collect-* pythonnet/webview`、排除 Qt 全家桶）：

```powershell
py -3.10 -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name PolitePuppy --icon dog_girl.ico `
  --add-data "index.html;." --add-data "dog_girl.png;." `
  --collect-all pythonnet --collect-data webview --collect-binaries webview `
  --hidden-import clr --hidden-import webview.platforms.winforms `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import pystray._win32 --hidden-import pystray._util.win32 `
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 `
  main.py
```

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 回复全是「嗯」「哦」 | 模型过小或温度过低。换 `qwen2.5:7b` 以上，温度 0.85–0.95 |
| 一直连不上 | `ollama serve` 是否在跑；`curl http://127.0.0.1:11434/api/tags` 是否有返回；模型名是否与 `ollama list` 完全一致 |
| 不发表情包 | 「设置 → 表情」先传图；可用名字会写进 System Prompt，名字对不上就发不出 |
| 从不主动开口 | 仅真实模型才会主动开腔（演示台词模式已关闭）；窗口收起时最多 20 分钟一次 |
| OneDrive 下 pythonnet 加载失败 | 已设 `COMPLUS_LoadFromRemoteSources=1`；仍失败则把目录迁到 `C:\PolitePuppy` |
| 启动白屏 | 安装 WebView2 Runtime |

---

## 状态

当前版本 **1.0**：聊天、语音、表情包、生图、记忆检索、唱歌台与扒歌学习、全局快捷键均已并入主程序并完成回归验证。唱歌台可在 exe 内直接调用，也可于 `puppy-sing/` 独立运行。

---

## 许可

[MIT](LICENSE) © 2026 oTATo666
