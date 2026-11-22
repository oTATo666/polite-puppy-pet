# GPT-SoVITS 接入说明（小狗实时克隆音色）

> 仓库：`RVC-Boss/GPT-SoVITS` 完整克隆（1065 个提交，HEAD `b4570b8`）
> 位置：放在自己的工作区目录里，**别放 OneDrive**（同步风暴会拖慢加载）；本说明不写死具体盘符。
> 参考音频：`voices/profiles/小狗/ref.wav`（4.35s / 24kHz / 单声道）

## 一、链路

```
桌宠 main.py ──/v1/audio/speech──▶ 小狗网关(9880) ──/tts(钉死小狗参考音)──▶ GPT-SoVITS api_v2(9881)
```

- `main.py` 与 `.env` **一行都不用改**：`.env` 的 `VOICE_TTS_URL=http://127.0.0.1:9880/v1/audio/speech`，
  网关把 openai 协议翻译成 api_v2 的 `/tts` 协议。
- **音色钉死**：参考音频与提示文本由网关统一注入（`tools/tts_ref.json`），调用方传什么 voice 都不好使，
  沿用音色身份证的思路——杜绝"换参数变成别人的声音"。
- **失败说清楚**：上游没起 / 模型没下 / 合成报错都返回明确原因，由 main.py 的严格克隆（`VOICE_STRICT_CLONE=1`）
  保持安静，绝不回退系统音。

## 二、小狗的参考配置（tools/tts_ref.json）

```json
{
  "ref_wav": "voices/profiles/小狗/ref.wav",
  "prompt_text": "小诺，今天也要元气满满哦，汪！",
  "text_lang": "zh", "prompt_lang": "zh", "media_type": "wav"
}
```

`ref_wav` 写相对路径时以程序目录为基准（也可以写绝对路径，但别把带用户名的路径提交进包）。

提示文本就是这段录音里说的话（来自素材包 README 的记录），**越准越像**。换参考音频时两处一起改。

## 三、首次启用（在你本机跑三步）

```bat
:: 1) 下载预训练模型（约 2-3 GB，官方同源 XXXXRT/GPT-SoVITS-Pretrained，国内默认 modelscope）
python tools\download_gsv_models.py

:: 2) 补齐引擎依赖（体检列出来的缺包；注意 transformers 需 <5）
::    先把 F5_PYTHON 指向克隆环境的 python.exe，再用它装
"%F5_PYTHON%" -m pip install funasr cn2an jieba jieba_fast g2p_en opencc sentencepiece -i https://pypi.tuna.tsinghua.edu.cn/simple

:: 3) 一键启动（自动再体检 → 起 api_v2:9881 → 起网关:9880）
python tools\start_puppy_gsv.py
```

任何一步有问题，`--check` 会给出精确缺失清单；下载慢可加 `--source hf-mirror`。

## 四、验证

```bat
python tools\start_puppy_gsv.py --check        :: 模型+依赖体检
python tools\puppy_tts_gateway.py --selftest   :: 网关体检（应显示"配置就绪"+上游在线）
:: 起服后：
curl http://127.0.0.1:9880/health              :: 网关+上游状态
curl http://127.0.0.1:9880/api/voice/id        :: 音色身份证（voice_id=puppy_小狗_e82c45be）
```

桌宠设置里语音引擎选克隆模式即可；每次出声，网关控制台会打印
`合成 N 字 → M 字节，耗时 Xms（ref=示例_任意文字.wav）`，可确认当前用的就是小狗参考音。

## 五、与 F5 的关系

F5-TTS 链路原样保留（`tools/f5_server.py`，9880 被 GSV 占用时 F5 请改端口或二选一）。
两套引擎共用同一个 `voices/profiles/小狗/` 音色档案；谁的 9880 在跑，桌宠就用谁。
GPT-SoVITS 的优势：参考音 3-10 秒即可零样本克隆、语速语调更自然（`speed_factor` 可调）、
api_v2 原生支持流式（`streaming_mode=true`，后续可升级为边合成边播，进一步压低首字延迟）。

## 六、文件清单（本次新增/修改）

| 文件 | 说明 |
|---|---|
| `tools/puppy_tts_gateway.py` | 小狗语音网关：双协议翻译 + 音色钉死 + 明确报错（9.3KB，纯标准库） |
| `tools/tts_ref.json` | 小狗参考音频配置（含提示文本） |
| `tools/download_gsv_models.py` | 官方同源模型下载器（modelscope 主源 / hf-mirror 备源，--check / --only） |
| `tools/start_puppy_gsv.py` | 一键启动编排器（体检→api_v2→网关→健康等待→Ctrl+C 收摊） |
| GPT-SoVITS 仓库 | 放在自己的工作区（非 OneDrive，避免同步风暴） |

`main.py`、`.env`、`index.html` 均未改动。
