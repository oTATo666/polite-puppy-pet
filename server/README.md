# 小狗桌宠 · 服务器部署包

把聊天 Web 服务部署到 Windows 服务器（宝塔面板），桌宠物 GUI 不上服务器。
**服务器模式零第三方依赖**：只需 Python 3.10+，不需要 pip install。

## 包内容

```
deploy.ps1          一键部署脚本（复制文件/写 .env/健康检查/输出守护配置）
start_server.bat    启动器（宝塔进程守护/NSSM 的运行命令填它）
.env.example        环境变量模板（说明每个变量含义）
app/                程序本体（源码 + 素材，服务器直接跑）
部署指南.md         完整版：反代/HTTPS/备份/排查/回滚
```

## 三步上线

```powershell
# 1) 服务器装好 Python 3.10+（勾选 Add to PATH），把本包上传解压，例如到 C:\deploy
# 2) 以管理员身份打开 PowerShell：
cd C:\deploy
powershell -ExecutionPolicy Bypass -File deploy.ps1 -Port 8765 -AllowedHosts "你的域名" -ApiKey "sk-你的密钥"
# 3) 看到 "HEALTH OK" 即部署成功，按脚本第 6 步提示在宝塔加进程守护 + 反代
```

## 验证

```powershell
curl http://127.0.0.1:8765/api/health
# {"ok":true,"model":"...","diary":true,...}
```

## 前置条件清单

| 项 | 要求 |
|---|---|
| Python | 3.10+，加入 PATH |
| LLM | 云端 API Key（服务器 2G 内存跑不了本地 Ollama） |
| 反代 | 宝塔站点反代 → 127.0.0.1:8765，发送域名 `$host`，关闭缓冲 |
| 记忆数据 | `memory_diary.txt` / `brain.json` 属隐私文件**不在包内**，需要的话手动放进 `app\` |
