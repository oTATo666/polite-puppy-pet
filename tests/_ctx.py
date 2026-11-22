# -*- coding: utf-8 -*-
"""测试公共准备：把数据目录指到临时目录，再 import main。

⚠ 顺序很重要：main 在 import 时就会算 DATA_DIR / LOG_FILE / CONFIG_FILE。
先设 POLITE_PUPPY_DATA，再 import，测试才不会往真的 %APPDATA% 和记忆库里写东西。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
if str(PROG) not in sys.path:
    sys.path.insert(0, str(PROG))

TMP = Path(tempfile.mkdtemp(prefix="puppy_test_"))
os.environ["POLITE_PUPPY_DATA"] = str(TMP / "appdata")

import main  # noqa: E402  （必须在设置环境变量之后）

# 记忆库 / 日记 / 日志 / 配置全部搬进临时目录
main.MEMORY_DIR = TMP / "记忆"
main.MEMORY_DB = main.MEMORY_DIR / "memory.db"
main.MEMORY_ORGANIZE_STATE = main.MEMORY_DIR / "organize_state.json"
main.DIARY_FILE = TMP / "memory_diary.txt"
main.LOG_FILE = TMP / "pet.log"
main.CONFIG_FILE = TMP / "settings.json"
main.DATA_DIR = TMP / "appdata"
main._MEM_INITED = False
main.setup_logging(False)

AGENT_DATA = TMP / "agent_data"
# N-19：完整数据目录层——真实环境是 data_dir()（记忆/日记/agent_data 的上一级），
# 测试里这几样都直接铺在 TMP 下，所以 TMP 就是它的等价物。
DATA_ROOT = TMP


def api_token() -> str:
    """P1-5 之后 /api/* 要带令牌；没这机制时返回空串，测试照样能跑。"""
    return str(getattr(main, "API_TOKEN", "") or "")


def api_headers(extra: dict | None = None) -> dict:
    out = {}
    token = api_token()
    if token:
        out["X-Puppy-Token"] = token
    out.update(extra or {})
    return out


# ---------------------------------------------------------------- 可选开发资产
# 下面几样只在「开发树 / 交付包」里存在，公开仓库刻意不带：
#   · brain.json         个人语料，永不入库（.gitignore 里有）
#   · release/persona.json  外发版中性人设，只有做发布时才拷进来
#   · 01_程序/           交付包布局，仓库是扁平结构
# 缺了就跳过相关用例——不是失败，是这套件本来就不该在缺料时报警。
HAS_BRAIN = (PROG / "brain.json").is_file()
HAS_RELEASE_PERSONA = (PROG / "release" / "persona.json").is_file()
PACKAGE_TREE = (PROG / "01_程序").is_dir()
_README = PROG / "README.md"
DOC_PLACEHOLDERS = (_README.is_file()
                    and "{{TESTS}}" in _README.read_text(encoding="utf-8",
                                                         errors="ignore"))

SKIP_NO_BRAIN = "缺少 brain.json（个人语料，不入公开仓库）"
SKIP_NO_RELEASE_PERSONA = "缺少 release/persona.json（外发版人设，只在发布时拷入）"
SKIP_NOT_PACKAGE = "当前是仓库扁平布局，不是 01_程序/ 交付包"
SKIP_NO_DOC_PLACEHOLDERS = "仓库 README 不走外发回填（没有 {{TESTS}} 占位符）"
