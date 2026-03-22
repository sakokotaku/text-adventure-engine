"""
utils/logger.py
---------------
统一日志初始化。只需在程序入口调用 setup() 一次，之后各模块各自：
    import logging
    logger = logging.getLogger(__name__)

日志写到:
  · logs/game.log   — INFO 级以上（debug=true 时 DEBUG 级）
  · stderr          — WARNING 级以上（不污染游戏 UI 的 stdout）

查看日志：
  Windows CMD:  type logs\\game.log
  PowerShell:   Get-Content logs\\game.log -Tail 50 -Wait
  Git Bash:     tail -f logs/game.log
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR       = _PROJECT_ROOT / "logs"
LOG_FILE      = LOG_DIR / "game.log"

# ── 格式 ──────────────────────────────────────────────────────────
_FMT      = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def setup(debug: bool = False, log_level: str = "INFO") -> None:
    """
    初始化日志系统（幂等，多次调用无副作用）。

    Args:
        debug:     True → 强制 DEBUG 级，覆盖 log_level
        log_level: "DEBUG" / "INFO" / "WARNING" / "ERROR"（大小写不敏感）
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # 创建 logs/ 目录
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 有效级别
    if debug:
        level = logging.DEBUG
    else:
        level = getattr(logging, log_level.upper(), logging.INFO)

    fmt = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    # ── 文件 handler：记录 level 及以上，追加模式 ─────────────────
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
    fh.setLevel(level)
    fh.setFormatter(fmt)

    # ── 终端 handler（stderr）：WARNING 及以上 ────────────────────
    # 只写 stderr，不混入游戏 stdout 的对话输出
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(fh)
    root.addHandler(ch)

    # 屏蔽 urllib3 / urllib 的低级噪音
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("urllib").setLevel(logging.WARNING)


def trunc(s: str, n: int = 120) -> str:
    """截断长字符串，用于日志摘要（避免日志条目过长）。"""
    s = str(s).replace("\n", " ")
    return s[:n] + f"…[+{len(s)-n}]" if len(s) > n else s
