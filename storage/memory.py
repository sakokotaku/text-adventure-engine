"""
storage/memory.py
-----------------
长期记忆层：event_cards 已改为按游戏日分组的滚动窗口（14天），
超出部分以纯字符串截取方式归档进 event_archive，不再调用 LLM。

compress_events / should_summarize 已停用，保留空壳以兼容旧导入。
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# 已停用——保留以兼容旧导入，调用者无需修改 import
SUMMARY_TRIGGER = 8
SUMMARY_COMPRESS_COUNT = 5


def should_summarize(world_state: dict) -> bool:  # noqa: ARG001
    """已停用：event_cards 改为按日分组，不再需要 LLM 压缩触发检测。"""
    return False


def compress_events(world_state: dict, generate_fn) -> dict:  # noqa: ARG001
    """已停用：直接返回原 world_state，不做任何修改。"""
    return world_state


def inject_summary_to_context(world_state: dict) -> str:  # noqa: ARG001
    """已停用：摘要不再注入 context。保留以兼容旧导入。"""
    return ""
