"""
storage/save_manager.py
-----------------------
存档格式 v4（三层）字段说明：

  系统元数据（程序写入，下划线前缀）：
    _saved_at   str        ISO 时间戳
    _label      str        存档标签（manual/auto/exit…）
    _history    list[dict] 本次会话最近 SESSION_WINDOW 轮对话

  ── 第一层：save_info（时间/地点元信息）──────────────────────
    save_info   dict  {turn, date, time_slot, location}

  ── 第二层：world_rules（不可变世界设定）─────────────────────
    world_rules dict  {setting, tone, player_scope}

  ── 第三层：characters（稳定角色数据库）──────────────────────
    characters  dict  {
      heroines: [{name, nickname, appearance, personality_core,
                  speech_samples, hidden_attributes, address,
                  first_reactions, private_anchors,
                  affection, relationship_stage, last_interaction}]
      supporting_characters: [...]
    }

  ── 第四层：story_state（动态叙事状态）───────────────────────
    story_state dict  {
      time, location,
      event_cards:     [{event, location, who_initiated, beats,
                         emotional_result, aftermath}]   # 最多10条
      recent_memory:   [str]                             # 最多5条
      suspended_issues:[{character, issue}]
    }

  gm_instructions  str   载入时 GM 必读规则

  ── 旧格式兼容字段（v1-v3，读取时自动识别）─────────────────
    world, player, heroines, supporting_characters,
    world_events, suspended_issues, gm_instructions,
    npc_memory, world_state, event_log
"""

from __future__ import annotations

import json
import glob
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
SAVE_DIR = BASE_DIR / "saves"


def _story_dir(story_name: str) -> Path:
    d = SAVE_DIR / f"story_{story_name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(
    story_name: str,
    state: dict,
    raw_json_str: str | None = None,
    label: str = "auto",
) -> Path:
    """
    写入存档。
    raw_json_str: GM 生成的 JSON 字符串（优先使用）；
                  为 None 时从 state["world_state"] 构建 fallback。
    state 必须包含: round, history, world_state
    """
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
    turn = 0
    data: dict = {}

    if raw_json_str:
        raw_json_str = raw_json_str.strip()
        try:
            data = json.loads(raw_json_str)
            turn = data.get("save_info", {}).get("turn", 0)
        except json.JSONDecodeError as e:
            # JSON 无效：以纯文本存储
            path = _story_dir(story_name) / f"save_{ts}_r0_{label}.txt"
            path.write_text(raw_json_str, encoding="utf-8")
            logger.warning("save() 内部 JSON 解析失败，降级为 txt：%s  原因：%s", path.name, e)
            return path
    else:
        # Fallback：从内存 world_state 复制已知字段
        ws   = state.get("world_state", {})
        turn = state.get("round", 0)
        # 新格式字段
        for key in ("save_info", "world_rules", "characters", "story_state", "gm_instructions"):
            if key in ws:
                data[key] = ws[key]
        # 旧格式兼容字段（含首回合设定文本，供跨会话恢复系统提示用）
        for key in ("world", "player", "heroines", "supporting_characters",
                    "world_events", "suspended_issues", "npc_memory",
                    "world_state", "event_log", "_initial_setting"):
            if key in ws:
                data[key] = ws[key]

    # ── 新格式没有 player 时，尝试从旧格式补全 ─────────────────
    if "player" not in data:
        w = data.get("world", {})
        if w.get("player_name") or w.get("player_identity"):
            data["player"] = {
                "name":     w.get("player_name", ""),
                "identity": w.get("player_identity", ""),
                "special":  "",
            }

    # ── 系统元数据（覆盖写入）──────────────────────────────────
    data["_history"]  = state.get("history", [])   # 短窗口，无长期历史
    data["_saved_at"] = datetime.now().isoformat()
    data["_label"]    = label
    if state.get("story_summary"):
        data["story_summary"] = state["story_summary"]

    content = json.dumps(data, ensure_ascii=False, indent=2)
    path    = _story_dir(story_name) / f"save_{ts}_r{turn}_{label}.json"
    path.write_text(content, encoding="utf-8")
    logger.info("存档写入：%s（%d 字节）", path.name, len(content.encode("utf-8")))
    _enforce_save_limit(story_name, limit=2)
    return path


def list_saves(story_name: str) -> list:
    """列出指定故事的所有存档（时间倒序）"""
    json_files = glob.glob(str(_story_dir(story_name) / "save_*.json"))
    txt_files  = glob.glob(str(_story_dir(story_name) / "save_*.txt"))
    files      = sorted(json_files + txt_files, reverse=True)
    result     = []
    for i, fp in enumerate(files):
        if fp.endswith(".txt"):
            result.append({
                "index":    i + 1,
                "path":     fp,
                "filename": Path(fp).name + "  [⚠️ 解析失败·文本备份]",
                "turn":     "?",
                "label":    "fallback",
                "saved_at": "",
            })
            continue
        try:
            data = json.loads(Path(fp).read_text(encoding="utf-8"))
            turn = data.get("save_info", {}).get("turn") or data.get("round", "?")
            result.append({
                "index":    i + 1,
                "path":     fp,
                "filename": Path(fp).name,
                "turn":     turn,
                "label":    data.get("_label", "auto"),
                "saved_at": data.get("_saved_at", ""),
            })
        except Exception as e:
            logger.warning("读取存档列表跳过损坏文件：%s  (%s)", Path(fp).name, e)
    return result


def load_latest(story_name: str) -> dict | None:
    """加载最新存档"""
    saves = list_saves(story_name)
    return load_by_path(saves[0]["path"]) if saves else None


def load_by_index(story_name: str, index: int) -> dict | None:
    """通过索引加载存档（1-based）"""
    saves = list_saves(story_name)
    if index < 1 or index > len(saves):
        return None
    return load_by_path(saves[index - 1]["path"])


def load_by_path(path: str) -> dict | None:
    """通过路径加载存档"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        logger.info("存档读取成功：%s", Path(path).name)
        return data
    except Exception as e:
        logger.warning("存档读取失败：%s  (%s)", Path(path).name, e)
        return None


def delete_save(story_name: str, index: int) -> bool:
    """删除指定编号的存档文件，返回是否成功。"""
    saves = list_saves(story_name)
    if index < 1 or index > len(saves):
        return False
    target = saves[index - 1]
    try:
        Path(target["path"]).unlink()
        logger.info("存档已删除：%s", target["filename"])
        return True
    except Exception as e:
        logger.warning("存档删除失败：%s  (%s)", target["filename"], e)
        return False


def list_stories() -> list:
    """列出所有故事名称"""
    if not SAVE_DIR.exists():
        return []
    return [
        d.name.replace("story_", "", 1)
        for d in SAVE_DIR.iterdir()
        if d.is_dir() and d.name.startswith("story_") and d.name != "story_"
    ]


def _enforce_save_limit(story_name: str, limit: int = 2) -> None:
    """
    保持每个故事最多 limit 条存档（时间倒序，保留最新的 limit 条）。
    超出部分从列表末尾（最旧）删除。
    """
    saves = list_saves(story_name)
    to_delete = saves[limit:]
    for s in to_delete:
        try:
            Path(s["path"]).unlink()
            logger.info("存档上限清理：删除旧存档 %s", s["filename"])
        except Exception as e:
            logger.warning("存档上限清理失败：%s  (%s)", s["filename"], e)
