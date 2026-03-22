"""
main.py — 通用叙事游戏引擎 v4.0
运行方式：python main.py

v4 变更：
  · 三层存档（world_rules / characters / story_state）
  · 对话历史改为短窗口（SESSION_WINDOW 轮），长期记忆由 event_cards + recent_memory 承载
  · 程序硬锁定角色核心属性，防止 AI 存档漂移
  · 移除摘要生成，改为 event_cards 结构化记忆
"""

from __future__ import annotations

import logging
import os
import sys
import json
import random as _rng
import re as _re

sys.path.insert(0, os.path.dirname(__file__))

# ── 日志（在其他模块 import 之前初始化，确保各模块 getLogger 能立刻使用）──
from utils.logger import setup as _setup_logging, trunc as _trunc, LOG_FILE as _LOG_FILE

logger = logging.getLogger(__name__)

from llm.provider import (
    generate,
    generate_with_history,
    is_streaming,
    is_debug,
    load_config,
    get_provider_cfg,
)
from prompt.builder import (
    build_system_prompt,
    build_static_system_prompt,
    build_dynamic_context,
    build_user_prompt,
)
from storage.save_manager import (
    save,
    load_by_path,
    load_by_index,
    list_saves,
    list_stories,
    delete_save,
)
from storage.memory import should_summarize, compress_events

AUTO_SAVE_EVERY = 10

# 每次 API 调用实际携带的最近轮数（新架构：短窗口，长期记忆靠 event_cards）
SESSION_WINDOW = 5   # turns = 10 messages max in state["history"]

# ─── 新游戏向导预设 ───────────────────────────────────────────────
_WORLD_PRESETS = [
    ("现代都市", "科技与日常生活交织的当代城市"),
    ("古代仙侠", "修仙界，灵气复苏，门派纷争"),
    ("西方奇幻", "魔法与剑，精灵矮人龙族并存"),
    ("科幻未来", "星际文明，高科技，AI并存"),
    ("末世废土", "文明崩溃后的荒芜世界"),
    ("校园青春", "高中或大学，青春恋爱日常"),
]

_TONE_PRESETS = [
    "轻松治愈",
    "浪漫恋爱",
    "悬疑剧情",
    "暗黑虐恋",
    "热血冒险",
]

_PERSONALITY_HINTS = "冷傲 / 活泼 / 温柔 / 腹黑 / 傲娇 / 神秘 / 强势 / 天然 / 呆萌 / 独立"


# A模式：自由文本解析辅助数据
_TONE_KEYWORDS_MAP = {
    "轻松": "轻松治愈", "治愈": "轻松治愈",
    "浪漫": "浪漫恋爱", "恋爱": "浪漫恋爱",
    "悬疑": "悬疑剧情", "推理": "悬疑剧情",
    "暗黑": "暗黑虐恋", "虐恋": "暗黑虐恋", "虐": "暗黑虐恋",
    "热血": "热血冒险", "冒险": "热血冒险", "战斗": "热血冒险",
}
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5}


def _parse_free_text(text: str) -> dict:
    """从自由文本中提取已知字段，未能识别的字段不包含在返回值中。

    返回 dict 可包含以下 key：
      tone              -> str（匹配到的基调）
      heroine_count     -> int（女主角数量）
      heroine_personalities -> list[str]（已提及的性格标签）
      player_identity   -> str（玩家职业/身份关键词）
    """
    result: dict = {}
    # 基调
    for kw, tone in _TONE_KEYWORDS_MAP.items():
        if kw in text:
            result["tone"] = tone
            break
    # 女主角数量
    m = _re.search(r"([一两二三四五1-5])\s*[个位名]?\s*(女主|女孩|女性|女生|女角)", text)
    if m:
        ch = m.group(1)
        result["heroine_count"] = _CN_NUM.get(ch, int(ch) if ch.isdigit() else 1)
    # 女主角性格关键词
    personality_hints_list = [p.strip() for p in _PERSONALITY_HINTS.split("/")]
    found = [p for p in personality_hints_list if p and p in text]
    if found:
        result["heroine_personalities"] = found
    # 玩家职业/身份（常见角色词）
    _role_pat = r"(侦探|探长|学生|医生|战士|法师|骑士|商人|工程师|程序员|教师|警察|间谍|刺客|魔法师|猎人|武士|剑客)"
    m_role = _re.search(_role_pat, text)
    if m_role:
        result["player_identity"] = m_role.group(1)
    return result


def _print_hint(text: str) -> None:
    """输出提示文字：终端支持颜色则用 dim 样式，否则普通输出。"""
    use_dim = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    for line in text.split("\n"):
        if use_dim:
            print(f"  \033[2m{line}\033[0m")
        else:
            print(f"  {line}")


# ══════════════════════════════════════════════════════════════════
# UI 工具
# ══════════════════════════════════════════════════════════════════

def print_sep():
    print("─" * 55)


def print_response(text: str) -> None:
    """打印 GM 回复，保留原文段落结构（仅空行处输出空行）。"""
    for line in text.split("\n"):
        if line.strip() == "":
            print()
        else:
            print(line)


def print_help():
    print_sep()
    print("  /save [备注]   手动存档")
    print("  /load          列出存档，选择读取（回滚）")
    print("  /delete        列出存档，选择删除")
    print("  /status        当前状态面板")
    print("  /new           重新开始（清空进度，启动向导）")
    print("  /exit          存档并退出")
    print("  /help          帮助")
    print_sep()


def _affection_bar(value, max_val=100, width=10):
    v = max(0, min(int(value or 0), max_val))
    filled = round(v / max_val * width)
    return "❤" * filled + "♡" * (width - filled)


# ══════════════════════════════════════════════════════════════════
# 状态辅助
# ══════════════════════════════════════════════════════════════════

def empty_state() -> dict:
    """返回一个干净的游戏状态（v4）。"""
    return {
        "round":       0,
        "world_state": {},   # 完整存档 JSON（world_rules / characters / story_state）
        "history":     [],   # 本次会话最近 SESSION_WINDOW 轮对话（不落盘长期历史）
    }


def _strip_dyn_from_history_entry(msg: dict) -> dict:
    """
    清理旧存档中 user 消息里的动态上下文前缀（_dyn）。
    旧格式每条 user 消息头部含完整 event_cards/GM指令等（~2000字），
    导致 history 超出字符预算，回合上下文被大量裁剪。
    清理后只保留场景锚点（【当前场景】）及玩家指令部分。
    """
    if msg.get("role") != "user":
        return msg
    content = msg.get("content", "")
    instr_idx = content.find("[玩家指令]")
    if instr_idx <= 0:
        return msg  # 已是紧凑格式，或为 GM 导演模式，原样保留
    # 在指令标记前寻找场景锚点
    scene_idx = content.rfind("【当前场景】", 0, instr_idx)
    cut = scene_idx if scene_idx >= 0 else instr_idx
    return {**msg, "content": content[cut:]}


def restore_state(data: dict) -> dict:
    """从存档 dict 恢复 state。"""
    state = empty_state()
    state["round"]       = data.get("save_info", {}).get("turn", 0)
    state["world_state"] = data
    # 恢复最近 SESSION_WINDOW 轮对话，为新会话提供少量上下文
    old_hist = data.get("_history", [])
    # 清理旧存档中 user 消息里膨胀的动态上下文前缀，再截窗口
    old_hist = [_strip_dyn_from_history_entry(m) for m in old_hist]
    state["history"] = old_hist[-(SESSION_WINDOW * 2):]
    # 立刻裁剪：防止旧存档膨胀历史在第一回合前传给GM
    _trim_history(state)
    return state


_HISTORY_CHAR_BUDGET = 8000   # history 总字符上限；user 消息已紧凑化（仅场景锚点+玩家指令），5轮约1500字，预算可覆盖~25轮


def _trim_history(state: dict) -> None:
    """
    双重裁剪：
      1. 条数上限：保留最近 SESSION_WINDOW 轮（10 条 messages）
      2. 字符上限：从最旧开始丢弃，直到总字符 <= _HISTORY_CHAR_BUDGET
    防止旧存档恢复或异常路径导致 history 膨胀撑爆上下文窗口。
    """
    h = state["history"]

    # 1. 条数裁剪
    max_msgs = SESSION_WINDOW * 2
    if len(h) > max_msgs:
        h = h[-max_msgs:]

    # 2. 字符总量裁剪（从最旧消息开始丢弃）
    while h and sum(len(m.get("content", "")) for m in h) > _HISTORY_CHAR_BUDGET:
        h = h[2:]   # 每次丢掉最旧的一轮（user + assistant 各一条）

    state["history"] = h


# ══════════════════════════════════════════════════════════════════
# AI 增量更新解析与合并
# ══════════════════════════════════════════════════════════════════

_SEPARATOR            = "---JSON---"
_ALLOWED_UPDATE_KEYS  = {"save_info", "new_event", "npc_update", "gm_instructions", "weight_updates"}
_ALLOWED_SAVE_INFO_KEYS = {"date", "time_slot", "location", "tension"}

# 在 apply_updates() 中处理，但不注入 build_dynamic_context() 的字段
# 新增字段如果不需要注入 context，必须在此显式声明，否则启动报错
_CONTEXT_SKIP_FIELDS = {"weight_updates", "new_event", "npc_update"}


def _validate_field_registry():
    """
    启动时验证字段注册一致性。
    确保 _ALLOWED_UPDATE_KEYS 中的每个字段：
    1. 在 apply_updates() 中有对应处理分支
    2. 在 build_dynamic_context() 中有渲染，或在 _CONTEXT_SKIP_FIELDS 中显式声明跳过
    不满足则抛出 RuntimeError，列出缺失字段。
    """
    import pathlib

    main_src = pathlib.Path(__file__).read_text(encoding="utf-8")
    builder_src = pathlib.Path(__file__).parent / "prompt" / "builder.py"
    builder_src = builder_src.read_text(encoding="utf-8")

    errors = []
    for field in _ALLOWED_UPDATE_KEYS:
        # 检查 apply_updates() 里有没有处理这个字段
        if f'"{field}"' not in main_src and f"'{field}'" not in main_src:
            errors.append(f"[缺失] apply_updates() 未处理字段: {field}")

        # 检查 build_dynamic_context() 有没有渲染，或在跳过名单里
        if field not in _CONTEXT_SKIP_FIELDS:
            if f'"{field}"' not in builder_src and f"'{field}'" not in builder_src:
                errors.append(f"[缺失] build_dynamic_context() 未渲染且未声明跳过: {field}")

    if errors:
        raise RuntimeError(
            "字段注册一致性检查失败，请补全以下字段的处理逻辑：\n" +
            "\n".join(errors)
        )


# ── NPC 自动注册（兜底机制，不依赖 LLM 输出 npc_update）────────────────────
# 命名事件：关键词"名字/姓名/叫/自称"后紧跟2-4字汉字名字
_RX_NAME_AFTER_KW = _re.compile(r'(?:名字|姓名|叫做?|自称)([\u4e00-\u9fff]{2,4})')
# 主语动词事件：事件开头为2-4字汉字+常见动词，表明该名字是NPC主语
_RX_SUBJ_VERB     = _re.compile(
    r'^([\u4e00-\u9fff]{2,4})'
    r'(?:说|在|对|到|帮|拒|离|等|自|拿|走|回|看|推|提|来|去|站|抬|点|笑|'
    r'伸|转|低|按|叹|皱|抱|进|出|跟|陪|邀|请|递|接|拉|带|找|追|让|摇|跑)'
)
# 非NPC词：不应被识别为角色名字的常见词汇
_NON_NPC_NAMES = frozenset({
    "主角", "玩家", "GM", "物业", "收银员", "路人", "陌生人",
    "大厅", "便利", "电梯", "楼层", "公寓", "成功",
})
# 名字不应以这些介词/连词开头
_NON_NAME_FIRST_CHARS = frozenset("与和从在对向被把将让使以")
# 名字不应以这些泛称词结尾
_NON_NAME_SUFFIXES = (
    "住户", "男子", "女子", "男人", "女人", "邻居",
    "服务员", "店员", "大妈", "大爷", "老板", "朋友", "同学", "同事",
)


def _is_valid_npc_name(name: str) -> bool:
    """判断候选词是否可能是 NPC 名字（过滤泛称、介词开头、陌生人系列等）。"""
    if name in _NON_NPC_NAMES:
        return False
    if "陌生" in name:
        return False
    if name[0] in _NON_NAME_FIRST_CHARS:
        return False
    if name.endswith(_NON_NAME_SUFFIXES):
        return False
    return True


def _find_self_report_name(event_str: str) -> "str | None":
    """
    从"XXX自报名字/自报姓名"模式中提取紧接关键词前的 NPC 名字。
    使用字符串定位而非 regex，避免贪婪匹配在"邻居沈知意自报姓名"中错取前缀。
    """
    for kw in ("自报名字", "自报姓名"):
        pos = event_str.find(kw)
        if pos < 2:
            continue
        # 优先取3字名，再试2字，再试4字
        for length in (3, 2, 4):
            if pos >= length:
                candidate = event_str[pos - length: pos]
                if _is_valid_npc_name(candidate):
                    return candidate
    return None


def _detect_npc_name(event_str: str) -> "tuple[str | None, bool]":
    """
    从事件字符串中提取候选 NPC 名字。
    返回 (name, is_naming_event)；is_naming_event=True 表示命名事件，应立即注册。
    """
    # 最高优先级：关键词后跟名字，如"名字林晚"
    m = _RX_NAME_AFTER_KW.search(event_str)
    if m and _is_valid_npc_name(m.group(1)):
        return m.group(1), True
    # 次级：NPC 自报姓名，如"沈知意自报姓名"
    name = _find_self_report_name(event_str)
    if name:
        return name, True
    # 三级：NPC 作为句子主语，如"林晚说她住15楼"
    m = _RX_SUBJ_VERB.match(event_str)
    if m and _is_valid_npc_name(m.group(1)):
        return m.group(1), False
    return None, False


def _auto_register_npc(world_state: dict, name: str) -> None:
    """若 name 尚未在任何角色列表中，自动在 supporting_characters 创建档案。
    同时从 event_cards 反向提取该 NPC 相关事件作为 player_knowledge 初始内容。"""
    chars = world_state.setdefault("characters", {})
    heroines   = chars.setdefault("heroines", [])
    supporting = chars.setdefault("supporting_characters", [])
    known = {h.get("name") for h in heroines} | {c.get("name") for c in supporting}
    if name in known:
        return

    si = world_state.get("save_info", {})

    # 从 event_cards 反向提取该 NPC 相关事件
    story = world_state.get("story_state", {})
    ec = story.get("event_cards", {})
    if isinstance(ec, dict):
        all_events = [e for evts in ec.values() for e in evts]
    elif isinstance(ec, list):
        all_events = [e for e in ec if isinstance(e, str)]
    else:
        all_events = []

    extracted_knowledge = []
    for event_str in all_events:
        if name in event_str:
            # 去掉 name 前缀，保留事件描述作为认知条目
            knowledge_item = event_str.replace(name, "").strip()
            # 过滤过短或无意义条目（少于4字）
            if len(knowledge_item) >= 4:
                extracted_knowledge.append(knowledge_item)

    # 去重，保留最多10条（避免档案膨胀）
    seen = set()
    deduped = []
    for item in extracted_knowledge:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    player_knowledge = deduped[:10]

    supporting.append({
        "name": name,
        "player_knowledge": player_knowledge,
        "relationship_milestones": [{
            "round":    si.get("turn", 0),
            "date":     si.get("date", "未知日期"),
            "location": si.get("location", "未知地点"),
            "event":    "初识",
            "detail":   "（系统自动档案）",
        }],
        "npc_relations": {},
    })
    logger.info(
        "auto_register_npc: 自动注册NPC '%s'  r=%d  提取知识条目=%d条",
        name, si.get("turn", 0), len(player_knowledge),
    )


def parse_response(raw: str) -> "tuple[str, dict]":
    """
    从 AI 原始回复中分离叙事文本和增量状态更新。
    返回 (narrative, updates)。
    容错：未输出分隔符 / JSON 格式错误 / 字段非法，均返回空 updates，游戏照常继续。
    """
    sep_idx = raw.find(_SEPARATOR)
    if sep_idx == -1:
        logger.warning("parse_response: 未检测到---JSON---，本回合跳过增量更新")
        return raw.strip(), {}

    narrative = raw[:sep_idx].strip()
    json_str  = raw[sep_idx + len(_SEPARATOR):].strip()

    if not json_str:
        return narrative, {}

    try:
        raw_updates = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("parse_response: JSON解析失败 %s | 原文: %s", e, json_str[:200])
        return narrative, {}

    if not isinstance(raw_updates, dict):
        return narrative, {}

    updates: dict = {}
    rejected = set(raw_updates.keys()) - _ALLOWED_UPDATE_KEYS
    if rejected:
        logger.warning("parse_response: 拒绝非法字段 %s", rejected)

    if "save_info" in raw_updates:
        si = raw_updates["save_info"]
        if isinstance(si, dict):
            clean_si = {k: v for k, v in si.items() if k in _ALLOWED_SAVE_INFO_KEYS}
            if clean_si:
                updates["save_info"] = clean_si

    if "new_event" in raw_updates:
        ne = raw_updates["new_event"]
        if isinstance(ne, str) and ne.strip():
            updates["new_event"] = ne.strip()[:100]

    if "gm_instructions" in raw_updates:
        gi = raw_updates["gm_instructions"]
        if isinstance(gi, str):
            updates["gm_instructions"] = gi.strip()[:500]  # 空字符串表示清除

    if "npc_update" in raw_updates:
        nu = raw_updates["npc_update"]
        if isinstance(nu, dict) and isinstance(nu.get("name"), str) and nu["name"].strip():
            clean_nu: dict = {"name": nu["name"].strip()}
            # player_knowledge：列表，每条截断至15字
            pk = nu.get("player_knowledge")
            if isinstance(pk, list):
                clean_nu["player_knowledge"] = [
                    str(item)[:15] for item in pk if isinstance(item, str) and str(item).strip()
                ]
            # npc_relations：列表，保留合法条目
            nr = nu.get("npc_relations")
            if isinstance(nr, list):
                clean_relations = []
                for rel in nr:
                    if isinstance(rel, dict) and isinstance(rel.get("name"), str):
                        clean_relations.append({
                            "name": rel["name"],
                            "relation": str(rel.get("relation", "")),
                            "attitude": str(rel.get("attitude", "")),
                            "knows_player_connection": bool(rel.get("knows_player_connection", False)),
                        })
                if clean_relations:
                    clean_nu["npc_relations"] = clean_relations
            # new_milestone：单个节点
            ms = nu.get("new_milestone")
            if isinstance(ms, dict):
                clean_nu["new_milestone"] = {
                    "round":    int(ms.get("round", 0)),
                    "date":     str(ms.get("date", "")),
                    "location": str(ms.get("location", "")),
                    "event":    str(ms.get("event", "")),
                    "detail":   str(ms.get("detail", "")),
                }
            updates["npc_update"] = clean_nu

    if "weight_updates" in raw_updates:
        wu_list = raw_updates["weight_updates"]
        if isinstance(wu_list, list):
            clean_wu = [
                {"name": str(wu["name"]), "delta": int(wu.get("delta", 0))}
                for wu in wu_list
                if isinstance(wu, dict) and isinstance(wu.get("name"), str)
            ]
            if clean_wu:
                updates["weight_updates"] = clean_wu

    logger.debug("parse_response: narrative=%d字  updates=%s", len(narrative), list(updates.keys()))
    return narrative, updates


def apply_updates(world_state: dict, updates: dict) -> None:
    """
    将 parse_response() 解析出的增量更新合并进 world_state（原地修改）。
    save_info.turn 由程序维护，AI 不可修改。
    """
    if not updates:
        return

    if "save_info" in updates:
        if "save_info" not in world_state:
            world_state["save_info"] = {}
        si = world_state["save_info"]
        for key in _ALLOWED_SAVE_INFO_KEYS:
            if key in updates["save_info"]:
                si[key] = updates["save_info"][key]
        logger.debug("apply_updates: save_info → %s", {k: world_state["save_info"].get(k) for k in ("date", "time_slot", "location", "tension")})

    if "new_event" in updates:
        if "story_state" not in world_state:
            world_state["story_state"] = {
                "event_cards": {}, "suspended_issues": []
            }
        story = world_state["story_state"]
        # 兼容旧格式：若 event_cards 仍是列表则迁移为空 dict
        if isinstance(story.get("event_cards"), list):
            story["event_cards"] = {}
        story.setdefault("event_cards", {})

        date = world_state.get("save_info", {}).get("date", "未知日期")
        ec = story["event_cards"]
        ec.setdefault(date, [])
        ec[date].append(updates["new_event"])

        # 超过14天时，直接丢弃最旧一天（不归档）
        if len(ec) > 14:
            oldest_date = next(iter(ec))
            ec.pop(oldest_date)

        logger.debug("apply_updates: event_cards=%d天  最新日期=%s",
                     len(ec), date)

        # 兜底：自动检测并注册新NPC，不依赖 LLM 输出 npc_update
        candidate, is_naming = _detect_npc_name(updates["new_event"])
        if candidate:
            if is_naming:
                _auto_register_npc(world_state, candidate)
            else:
                # 非命名事件：该名字在 event_cards 中出现 2+ 次才注册
                all_events = [e for evts in ec.values() for e in evts]
                if sum(1 for e in all_events if candidate in e) >= 2:
                    _auto_register_npc(world_state, candidate)

    if "npc_update" in updates:
        nu = updates["npc_update"]
        target_name = nu["name"]
        # 在 heroines 和 supporting_characters 中寻找目标 NPC
        chars = world_state.get("characters", {})
        npc_lists = [
            chars.get("heroines", []),
            chars.get("supporting_characters", []),
            world_state.get("heroines", []),   # 旧格式兼容
        ]
        target = None
        for npc_list in npc_lists:
            for npc in npc_list:
                if isinstance(npc, dict) and npc.get("name") == target_name:
                    target = npc
                    break
            if target is not None:
                break

        if target is not None:
            # player_knowledge：合并去重
            if "player_knowledge" in nu:
                existing_pk = target.setdefault("player_knowledge", [])
                for item in nu["player_knowledge"]:
                    if item not in existing_pk:
                        existing_pk.append(item)

            # npc_relations：按 name 匹配，存在则更新，不存在则新增
            if "npc_relations" in nu:
                existing_nr = target.setdefault("npc_relations", [])
                existing_by_name = {r["name"]: r for r in existing_nr if isinstance(r, dict)}
                for rel in nu["npc_relations"]:
                    if rel["name"] in existing_by_name:
                        existing_by_name[rel["name"]].update(rel)
                    else:
                        existing_nr.append(rel)

            # new_milestone：追加，不去重
            if "new_milestone" in nu:
                target.setdefault("relationship_milestones", []).append(nu["new_milestone"])

            logger.debug("apply_updates: npc_update 已应用到 %s", target_name)
        else:
            logger.warning("apply_updates: npc_update 找不到NPC '%s'，跳过", target_name)

    if "gm_instructions" in updates:
        gi = updates["gm_instructions"]
        if gi:
            world_state["gm_instructions"] = gi
            logger.debug("apply_updates: gm_instructions 已更新，长度 %d", len(gi))
        else:
            # 空字符串表示清除指令
            world_state.pop("gm_instructions", None)
            logger.debug("apply_updates: gm_instructions 已清除")

    if "weight_updates" in updates:
        chars = world_state.get("characters", {})
        all_heroines = (
            chars.get("heroines", [])
            or world_state.get("heroines", [])
        )
        # 本回合出场的 heroine 名称集合（用于更新 consecutive_absent）
        appeared_names: set[str] = set()

        for wu in updates["weight_updates"]:
            name  = wu.get("name", "")
            delta = wu.get("delta", 0)
            for h in all_heroines:
                if not isinstance(h, dict) or h.get("name") != name:
                    continue
                aw = h.setdefault("appearance_weight", {"value": 10, "consecutive_absent": 0})
                aw["value"] = max(1, min(20, aw.get("value", 10) + delta))
                appeared_names.add(name)
                logger.debug(
                    "apply_updates: weight_updates %s delta=%+d → value=%d",
                    name, delta, aw["value"],
                )
                break

        # 更新 consecutive_absent：出场归零，未出场+1
        for h in all_heroines:
            if not isinstance(h, dict):
                continue
            aw = h.setdefault("appearance_weight", {"value": 10, "consecutive_absent": 0})
            if h.get("name") in appeared_names:
                aw["consecutive_absent"] = 0
            else:
                aw["consecutive_absent"] = aw.get("consecutive_absent", 0) + 1


# ══════════════════════════════════════════════════════════════════
# AI 写入锁定（核心防漂移机制）
# ══════════════════════════════════════════════════════════════════

# 角色这些字段由程序保护，AI 存档即使修改了也会被覆盖回原值
_LOCKED_HEROINE_FIELDS = (
    "appearance", "personality_core", "speech_samples",
    "hidden_attributes", "address", "first_reactions",
    "private_anchors", "nickname",
)


def _enforce_locks(new_data: dict, old_data: dict) -> None:
    """
    AI 写入锁定：
      · world_rules 整块锁定
      · 角色 locked 字段还原（AI 只能写 affection / relationship_stage / last_interaction）
      · 自动裁剪 event_cards（≤10）/ recent_memory（≤5）
    """
    # 1. world_rules 完全锁定
    old_rules = old_data.get("world_rules", {})
    if old_rules:
        new_data["world_rules"] = old_rules

    # 1b. 旧格式 player.special 锁定
    #     GM 通过对话赋予的特殊能力（如读心术）记录在此字段，
    #     AI 存档时若内存中无对应信息就会清空为 ""，导致设定丢失。
    old_player = old_data.get("player", {})
    new_player = new_data.get("player", {})
    if old_player.get("special") and new_player is not None:
        if not new_player.get("special"):
            new_player["special"] = old_player["special"]
            logger.debug("_enforce_locks: 还原 player.special = %s", old_player["special"])

    # 2. 找旧存档的角色（支持新旧格式）
    old_chars  = (
        old_data.get("characters", {}).get("heroines", [])
        or old_data.get("heroines", [])
    )
    old_by_name = {h.get("name"): h for h in old_chars if h.get("name")}

    # 找新存档的角色
    new_chars = (
        new_data.get("characters", {}).get("heroines", [])
        or new_data.get("heroines", [])
    )
    for h in new_chars:
        name = h.get("name", "")
        if name in old_by_name:
            old_h = old_by_name[name]
            for field in _LOCKED_HEROINE_FIELDS:
                if field in old_h:
                    h[field] = old_h[field]   # 强制还原

    # 3. 裁剪 story_state：event_cards 已改为按日分组的 dict，不做 list 裁剪
    story = new_data.get("story_state", {})
    ec = story.get("event_cards")
    if isinstance(ec, list):
        # 旧格式兼容：列表形式迁移为 dict（以"未知日期"为键）
        story["event_cards"] = {"未知日期": ec[-10:]} if ec else {}
    if story.get("recent_memory"):
        story["recent_memory"] = story["recent_memory"][-5:]

    # 4. 旧格式 memory（mid/long）反幻觉校验
    #    只保留旧存档白名单中已有的条目，AI 新增的一律拒绝。
    #    新条目应由 AI 在下次存档时从 recent 自然降级产生，而非凭空写入。
    old_mem = old_data.get("memory", {})
    new_mem = new_data.get("memory", {})
    if old_mem and new_mem:
        _old_mid_set  = set(old_mem.get("mid",  []))
        _old_long_set = set(old_mem.get("long", []))

        clean_mid  = [e for e in new_mem.get("mid",  []) if e in _old_mid_set]
        clean_long = [e for e in new_mem.get("long", []) if e in _old_long_set]

        if len(clean_mid)  < len(new_mem.get("mid",  [])):
            logger.warning("_enforce_locks: 移除 %d 条幻觉 memory.mid 条目",
                           len(new_mem["mid"]) - len(clean_mid))
        if len(clean_long) < len(new_mem.get("long", [])):
            logger.warning("_enforce_locks: 移除 %d 条幻觉 memory.long 条目",
                           len(new_mem["long"]) - len(clean_long))

        new_mem["mid"]  = clean_mid
        new_mem["long"] = clean_long


# ══════════════════════════════════════════════════════════════════
# 存档 / 读档
# ══════════════════════════════════════════════════════════════════


def _build_scene_anchor(history: list, maxlen: int = 100) -> str:
    """从 history 中提取最后一条 GM 消息的开头，做空间锚定。

    history 为空或无 assistant 消息时返回空字符串。
    只取开头 maxlen 字（场景定位信息），不取末尾，避免叙事惯性。
    """
    last_gm = next(
        (m["content"] for m in reversed(history) if m["role"] == "assistant"),
        None,
    )
    return f"【当前场景】{last_gm[:maxlen]}\n" if last_gm else ""


def do_save(state: dict, story_name: str, label: str = "manual") -> None:
    """直接序列化 world_state 到存档文件。AI 增量更新已在每回合 apply_updates() 中完成，无需 LLM 调用。"""
    logger.info("开始存档：story=%s  label=%s  回合=%d", story_name, label, state.get("round", 0))

    ws = state.get("world_state", {})
    # save_info.turn 由程序维护，确保与实际回合数同步
    ws.setdefault("save_info", {})["turn"] = state.get("round", 0)

    try:
        path  = save(story_name, state, raw_json_str=None, label=label)
        fname = path.name if hasattr(path, "name") else os.path.basename(str(path))
        logger.info("存档完成（JSON）：%s", fname)
        print(f"  已存档：{fname}")
    except Exception as e:
        err = str(e)
        if len(err) > 300:
            err = err[:300] + " …[已截断]"
        logger.error("存档失败：%s", err)
        print(f"  存档失败：{err}")
        if is_debug():
            import traceback
            traceback.print_exc()


def do_load(story_name: str, state: dict) -> None:
    """列出存档，让用户选择回滚到任意一个。"""
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return
    print_sep()
    for s in saves:
        print(
            f"  [{s['index']:>2}] 第{s['turn']:>3}回合 | "
            f"{s['label']:<12} | {s['saved_at'][:16]}"
        )
    print_sep()
    choice = input("  输入编号回滚（回车取消）：").strip()
    if not choice.isdigit():
        return
    data = load_by_index(story_name, int(choice))
    if not data:
        print("  读取失败")
        return
    restored = restore_state(data)
    state.update(restored)
    print(
        f"  已回滚到第 {state['round']} 回合存档"
        f"（历史 {len(state['history'])} 条）"
    )


def do_delete(story_name: str) -> None:
    """列出存档，让用户选择删除。"""
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return
    print_sep()
    for s in saves:
        print(
            f"  [{s['index']:>2}] 第{s['turn']:>3}回合 | "
            f"{s['label']:<12} | {s['saved_at'][:16]}"
        )
    print_sep()
    choice = input("  输入要删除的编号（回车取消）：").strip()
    if not choice.isdigit():
        return
    idx = int(choice)
    saves_again = list_saves(story_name)
    if idx < 1 or idx > len(saves_again):
        print("  编号无效")
        return
    target = saves_again[idx - 1]
    confirm = input(
        f"  确认删除：第{target['turn']}回合 / {target['label']} / "
        f"{target['saved_at'][:16]} ？(y / 回车取消) > "
    ).strip()
    if confirm.lower() != "y":
        return
    if delete_save(story_name, idx):
        print(f"  已删除：{target['filename']}")
    else:
        print("  删除失败")


# ══════════════════════════════════════════════════════════════════
# 状态面板
# ══════════════════════════════════════════════════════════════════

def print_status(state: dict) -> None:
    ws = state.get("world_state", {})
    print_sep()
    print(f"  当前回合：第 {state['round']} 回合")
    print(f"  会话历史：{len(state['history'])} 条（窗口 {SESSION_WINDOW} 轮）")
    try:
        config = load_config()
        pcfg   = get_provider_cfg(config=config)
        pname  = config.get("active_provider", "?")
        print(f"  当前模型：{pname} / {pcfg.get('model', '?')}")
    except Exception as e:
        logger.warning("读取模型配置失败：%s", e)
        print("  当前模型：（配置读取失败）")
    if ws.get("save_info"):
        si = ws["save_info"]
        print(f"  游戏时间：{si.get('date','')} {si.get('time_slot','')} @ {si.get('location','')}")

    # 世界设定
    wr = ws.get("world_rules", {}) or ws.get("world", {})
    if wr:
        print(f"  世界背景：{wr.get('setting', '') or wr.get('background', '')}")

    # 角色好感
    chars    = ws.get("characters", {})
    heroines = chars.get("heroines", []) or ws.get("heroines", [])
    if heroines:
        print()
        print("  ─── 角色状态 ─────────────────────────────")
        for h in heroines:
            name  = h.get("name", "?")
            aff   = h.get("affection", 0)
            stage = h.get("relationship_stage") or h.get("stage", "")
            bar   = _affection_bar(aff)
            print(f"  {name:<6} {bar} {aff:>3}  {stage}")

    # 事件卡 & 最近记忆
    story = ws.get("story_state", {})
    event_cards   = story.get("event_cards", [])
    recent_memory = story.get("recent_memory", [])
    suspended     = story.get("suspended_issues", []) or ws.get("suspended_issues", [])

    if event_cards:
        print(f"\n  事件卡数量：{len(event_cards)} 条")

    if recent_memory:
        print()
        print("  ─── 最近记忆 ─────────────────────────────")
        for rm in recent_memory[-3:]:
            print(f"  · {rm}")

    if suspended:
        print()
        print("  ─── 待处理事项 ───────────────────────────")
        for iss in suspended[:3]:
            if isinstance(iss, dict):
                print(f"  · {iss.get('character','')}：{iss.get('issue','')}")
            else:
                print(f"  · {iss}")
    print_sep()


# ══════════════════════════════════════════════════════════════════
# 故事选择（平铺存档列表）
# ══════════════════════════════════════════════════════════════════

def _build_save_entries() -> list:
    entries = []
    for story in sorted(list_stories()):
        for s in list_saves(story):
            entries.append({"story": story, "save": s})
    return entries


def _print_main_menu(entries: list) -> int:
    print_sep()
    if entries:
        for i, e in enumerate(entries, 1):
            s     = e["save"]
            saved = s["saved_at"][:16].replace("T", " ") if s["saved_at"] else "─────────────"
            print(
                f"  [{i:>2}] {e['story']:<12}· 第{s['turn']:>3}回合  "
                f"{s['label']:<14}{saved}"
            )
    else:
        print("  （暂无存档，请新建故事）")
    new_idx = len(entries) + 1
    print()
    print(f"  [{new_idx:>2}] 新建故事")
    print(f"  [  0] 退出")
    if entries:
        print(f"  提示：d<编号> 删除该存档，如 d1")
    print_sep()
    return new_idx


def _input_story_name() -> str:
    while True:
        name = input("  新故事名（字母/数字/中文，回车默认 story1）：").strip()
        if not name:
            return "story1"
        if name.startswith("/"):
            print("  故事名不能以 / 开头")
            continue
        if any(c in name for c in r'\/:*?"<>|'):
            print("  故事名含非法字符，请重新输入")
            continue
        return name


def choose_or_create_story() -> tuple:
    while True:
        entries = _build_save_entries()
        new_idx = _print_main_menu(entries)
        choice  = input("  输入编号：").strip()
        if not choice:
            continue
        if choice == "0":
            print("  再见。")
            sys.exit(0)
        if choice.lower() == "/help":
            print_sep()
            print("  <编号>    读取存档继续游戏")
            print(f"  {new_idx}         新建故事")
            print("  d<编号>   删除存档，如 d1")
            print("  0         退出程序")
            print_sep()
            continue
        if choice.startswith("/"):
            print("  游戏内命令请进入故事后使用")
            continue
        if choice.lower().startswith("d") and choice[1:].isdigit():
            didx = int(choice[1:])
            if 1 <= didx <= len(entries):
                e     = entries[didx - 1]
                s     = e["save"]
                saved = s["saved_at"][:16].replace("T", " ") if s["saved_at"] else ""
                confirm = input(
                    f"  确认删除【{e['story']}】第{s['turn']}回合 / {s['label']} / {saved}？"
                    f"(y / 回车取消) > "
                ).strip()
                if confirm.lower() == "y":
                    if delete_save(e["story"], s["index"]):
                        print(f"  已删除：{s['filename']}")
                    else:
                        print("  删除失败")
            else:
                print(f"  编号无效，范围 1-{len(entries)}" if entries else "  暂无存档")
            continue
        if not choice.isdigit():
            print(f"  请输入 0-{new_idx} 之间的数字，或 d<编号> 删除存档")
            continue
        idx = int(choice)
        if 1 <= idx <= len(entries):
            e    = entries[idx - 1]
            data = load_by_path(e["save"]["path"])
            if data is None:
                print("  读取存档失败，文件可能已损坏")
                continue
            return e["story"], data
        if idx == new_idx:
            return _input_story_name(), None
        print(f"  编号无效，请输入 0-{new_idx}")


# ══════════════════════════════════════════════════════════════════
# 新游戏向导
# ══════════════════════════════════════════════════════════════════

_BACK = "__BACK__"

_TONE_CONFLICTS: list = [
    (frozenset({"轻松治愈", "暗黑虐恋"}),
     "「轻松治愈」与「暗黑虐恋」基调相反，组合可能导致叙事割裂"),
    (frozenset({"轻松治愈", "悬疑剧情"}),
     "「轻松治愈」与「悬疑剧情」组合较跳跃，整体气氛需把握"),
]


def _cancel(v: str) -> bool:
    return v.lower() == "q"


def _wz_mode_select() -> "str | None":
    """向导第0步：选择设定模式。返回 'A'（自由输入）/'B'（引导设定）/None（取消）。"""
    print("\n  【选择设定模式】")
    print("  " + "─" * 38)
    print("  [A] 自由输入    直接描述你想要的世界")
    print("  [B] 引导设定    一步步完成世界构建")
    print("  [q] 取消向导")
    while True:
        raw = input("\n  请选择 [默认B] > ").strip().upper()
        if not raw:
            return "B"
        if raw == "Q":
            return None
        if raw in ("A", "B"):
            return raw
        print("  请输入 A 或 B")


def _is_back(v: str) -> bool:
    return v.lower() in ("b", "back", "返回", "上一步")


def _is_rand(v: str) -> bool:
    return v.lower() in ("r", "随机")


def _parse_tone(raw: str) -> "tuple[str, str]":
    if _is_rand(raw):
        return _rng.choice(_TONE_PRESETS), ""
    parts = [p.strip() for p in _re.split(r"[+＋]", raw) if p.strip()]
    resolved: list = []
    extra: list = []
    for p in parts:
        if p.isdigit():
            idx = int(p)
            if 1 <= idx <= len(_TONE_PRESETS):
                resolved.append(_TONE_PRESETS[idx - 1])
            else:
                extra.append(p)
        elif p in _TONE_PRESETS:
            resolved.append(p)
        else:
            extra.append(p)
    if not resolved and not extra:
        return _TONE_PRESETS[1], ""
    warning = ""
    resolved_set = frozenset(resolved)
    for conflict_set, msg in _TONE_CONFLICTS:
        if conflict_set.issubset(resolved_set):
            warning = msg
            break
    combined = "+".join(resolved + extra)
    return combined or _TONE_PRESETS[1], warning


def _wz_world() -> "dict | str | None":
    print("\n  【①】世界背景")
    print("  " + "─" * 38)
    for i, (name, desc) in enumerate(_WORLD_PRESETS, 1):
        print(f"  [{i}] {name:<8}  {desc}")
    # [7] 完全自定义 已移除：可在输入框直接输入文字描述背景
    print("  [r] 随机  |  [q] 取消向导")
    while True:
        raw = input("\n  选择编号，或直接输入自定义背景描述 [默认1] > ").strip()
        if not raw:
            n, d = _WORLD_PRESETS[0]
            return {"world_bg": f"{n}——{d}"}
        if _cancel(raw):
            return None
        if _is_back(raw):
            print("  已是第一步，无法继续返回")
            continue
        if _is_rand(raw):
            n, d = _rng.choice(_WORLD_PRESETS)
            print(f"  → 随机：{n}")
            return {"world_bg": f"{n}——{d}"}
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(_WORLD_PRESETS):
                n, d = _WORLD_PRESETS[idx - 1]
                return {"world_bg": f"{n}——{d}"}
            print(f"  请输入 1-{len(_WORLD_PRESETS)}，r，q，或直接描述背景")
            continue
        return {"world_bg": raw}


def _wz_player() -> "dict | str | None":
    print("\n  【②】玩家身份")
    print("  " + "─" * 38)
    print("  [b] 返回上一步  |  [r] 随机  |  [q] 取消向导")
    while True:
        raw = input("  角色名字 [主角] > ").strip()
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        player_name = "随机" if _is_rand(raw) else (raw or "主角")
        break
    while True:
        raw = input("  身份/背景简介 [普通人] > ").strip()
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        player_identity = "随机" if _is_rand(raw) else (raw or "普通人")
        break
    raw = input("  特殊能力/技能（可选，回车跳过）> ").strip()
    if _cancel(raw): return None
    if _is_back(raw): return _BACK
    return {
        "player_name":     player_name,
        "player_identity": player_identity,
        "player_special":  raw,
    }


def _wz_heroines() -> "dict | str | None":
    print("\n  【③】女主角设定")
    print("  " + "─" * 38)
    print("  女主角设定方式：")
    print("  [1] 随机        数量和性格全部随机")
    print("  [2] 指定        自己决定数量")
    print("  [b] 返回上一步  |  [q] 取消向导")
    while True:
        raw = input("\n  选择 [默认1] > ").strip()
        if not raw or raw == "1":
            # 全随机
            hcount = _rng.randint(1, 3)
            heroines = [("随机", "随机", "") for _ in range(hcount)]
            print(f"  → 随机生成 {hcount} 位女主角")
            return {"heroines": heroines}
        if _cancel(raw):
            return None
        if _is_back(raw):
            return _BACK
        if raw == "2":
            break
        print("  请输入 1 或 2")
    # 指定模式：先问数量
    print(f"\n  性格参考：{_PERSONALITY_HINTS}")
    while True:
        raw = input("  女主角数量 [1-4，默认1 / r=随机] > ").strip()
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        if not raw:
            hcount = 1; break
        if _is_rand(raw):
            hcount = _rng.randint(1, 3)
            print(f"  → 随机数量：{hcount}"); break
        m = _re.search(r"[1-4]", raw)
        if m:
            hcount = int(m.group()); break
        print("  请输入 1-4 之间的数字（或含数字描述），r 随机")
    # 询问是否填写细节
    detail_raw = input("  是否设定性格/名字等细节？[y/n，默认n] > ").strip().lower()
    if _cancel(detail_raw):
        return None
    if detail_raw == "y":
        # 逐个填写详情
        heroines: list = []
        hi = 0
        while hi < hcount:
            print(f"\n  ─ 女主角 {hi + 1} ─")
            raw = input(f"  名字 [女主{hi + 1} / r=随机] > ").strip()
            if _cancel(raw): return None
            if _is_back(raw):
                if hi == 0: return _BACK
                hi -= 1; heroines.pop(); continue
            h_name = "随机" if _is_rand(raw) else (raw or f"女主{hi + 1}")
            raw = input("  性格标签 [神秘 / r=随机 / b=重填此角色] > ").strip()
            if _cancel(raw): return None
            if _is_back(raw): continue
            h_personality = "随机" if _is_rand(raw) else (raw or "神秘")
            raw = input("  外貌/背景补充（可选，回车跳过 / r=随机）> ").strip()
            if _cancel(raw): return None
            if _is_back(raw): continue
            h_desc = "随机" if _is_rand(raw) else raw
            heroines.append((h_name, h_personality, h_desc))
            hi += 1
    else:
        # 名字和性格全部随机
        heroines = [("随机", "随机", "") for _ in range(hcount)]
        print(f"  → {hcount} 位女主角将随机生成姓名与性格")
    return {"heroines": heroines}


def _wz_tone() -> "dict | str | None":
    print("\n  【④】游戏基调")
    print("  " + "─" * 38)
    for i, t in enumerate(_TONE_PRESETS, 1):
        print(f"  [{i}] {t}")
    print("  [r] 随机  |  支持组合：如 3+角色可攻略 / 2+5")
    print("  [b] 返回上一步  |  [q] 取消向导")
    while True:
        raw = input("\n  选择编号（或组合）[默认2] > ").strip()
        if not raw: raw = "2"
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        tone, warning = _parse_tone(raw)
        if warning:
            print(f"\n  ⚠  {warning}")
            confirm = input("  回车确认此组合 / b=重新选择 > ").strip()
            if _is_back(confirm): continue
        return {"tone": tone}


def _wz_plot() -> "dict | str | None":
    print("\n  【⑤】主线剧情（可选）")
    print("  " + "─" * 38)
    print("  [1] 随GM自由发挥")
    print("  [2] 有明确主线")
    print("  [r] 随机主线")
    print("  [b] 返回上一步  |  [q] 取消向导")
    while True:
        raw = input("\n  选择 [默认1] > ").strip()
        if not raw or raw == "1": return {"main_plot": ""}
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        if _is_rand(raw): return {"main_plot": "随机"}
        if raw == "2":
            while True:
                plot = input("  主线剧情简述 [r=随机 / b=返回] > ").strip()
                if _cancel(plot): return None
                if _is_back(plot): break
                if _is_rand(plot): return {"main_plot": "随机"}
                if plot: return {"main_plot": plot}
                print("  请输入剧情描述，r 随机，或选 1 跳过主线")
            continue
        return {"main_plot": raw}


def _wz_build_instruction(structured: dict) -> str:
    """根据结构化设定生成发送给GM的开局指令文本。"""
    world_bg        = structured["world_bg"]
    player_name     = structured["player_name"]
    player_identity = structured["player_identity"]
    player_special  = structured.get("player_special", "")
    heroines        = structured["heroines"]
    tone            = structured["tone"]
    main_plot       = structured.get("main_plot", "")
    lines = [
        "以下是本次游戏的完整世界设定，请严格按照设定展开故事：",
        "",
        f"【世界背景】{world_bg}",
    ]
    ident = f"【玩家身份】{player_name}——{player_identity}"
    if player_special:
        ident += f"（特殊能力：{player_special}）"
    lines.append(ident)
    lines.append("")
    lines.append("【女主角】")
    for h_name, h_personality, h_desc in heroines:
        entry = f"  · {h_name}：{h_personality}性格"
        if h_desc:
            entry += f"，{h_desc}"
        lines.append(entry)
    lines.append("")
    lines.append(f"【游戏基调】{tone}")
    if main_plot:
        lines.append(f"【主线剧情】{main_plot}")
    lines.append("")
    lines.append("请直接开始第一个场景，让玩家与第一位女主角自然相遇，无需询问任何设定问题。")
    return "\n".join(lines)


def _wz_show_summary(structured: dict) -> "tuple[str, dict] | str | None":
    """展示世界观确认摘要，玩家确认或修改后返回结果。

    返回值：
      (instruction, structured)  → 正常确认
      _BACK                      → 玩家选 b=重头重设，由 run_new_game_wizard 处理
      None                       → 玩家取消
    """
    instruction = _wz_build_instruction(structured)
    print("\n" + "═" * 55)
    print("  世界观确认摘要：")
    print("─" * 55)
    print(instruction)
    print("═" * 55)
    confirm = input("  回车发送给GM / 输入内容替换 / b=重头重设 / q=取消\n  > ").strip()
    if _cancel(confirm):
        return None
    if _is_back(confirm):
        return _BACK
    final_instruction = confirm if confirm else instruction
    return final_instruction, structured


def _wz_free_input() -> "tuple[str, dict] | str | None":
    """A模式：自由输入 → 解析 → 最小化追问 → 摘要确认。

    返回值：
      (instruction, structured)  → 完成
      _BACK                      → 用户在摘要处选 b（由 run_new_game_wizard 处理）
      None                       → 取消
    """
    print("\n  【自由描述模式】")
    print("  " + "─" * 38)
    _print_hint("💡 你可以描述：世界观、玩家身份、女主数量与性格、游戏基调")
    _print_hint("     例：\"18世纪伦敦的侦探，有两个女主，悬疑基调\"")
    while True:
        raw = input("\n  > ").strip()
        if not raw:
            print("  请输入描述内容")
            continue
        if _cancel(raw):
            return None
        break
    parsed = _parse_free_text(raw)
    world_bg = raw  # 自由文本整体作为世界背景描述

    # 玩家名字（难以从文本推断，始终询问）
    raw_name = input("\n  玩家名字（回车随机）> ").strip()
    if _cancel(raw_name):
        return None
    player_name = raw_name or "随机"

    # 玩家身份：若已推断则跳过，否则询问
    if "player_identity" in parsed:
        player_identity = parsed["player_identity"]
        print(f"  → 推断玩家身份：{player_identity}")
    else:
        raw_ident = input("  玩家身份/职业（回车根据世界观随机推断）> ").strip()
        if _cancel(raw_ident):
            return None
        player_identity = raw_ident or "随机"

    # 游戏基调：若已推断则跳过，否则询问
    if "tone" in parsed:
        tone = parsed["tone"]
        print(f"  → 推断游戏基调：{tone}")
    else:
        print("\n  游戏基调（未能从描述中识别）：")
        for i, t in enumerate(_TONE_PRESETS, 1):
            print(f"  [{i}] {t}")
        while True:
            traw = input("  选择编号（回车随机）> ").strip()
            if _cancel(traw):
                return None
            if not traw:
                tone = _rng.choice(_TONE_PRESETS)
                break
            if traw.isdigit() and 1 <= int(traw) <= len(_TONE_PRESETS):
                tone = _TONE_PRESETS[int(traw) - 1]
                break
            tone = traw
            break

    # 女主角处理
    if "heroine_count" in parsed:
        count = max(1, min(4, parsed["heroine_count"]))
        personalities = parsed.get("heroine_personalities", [])
        heroines = []
        for i in range(count):
            h_personality = personalities[i] if i < len(personalities) else "随机"
            heroines.append(("随机", h_personality, ""))
    else:
        heroines = [("随机", "随机", "")]  # 默认1个随机女主

    structured = {
        "world_bg":        world_bg,
        "player_name":     player_name,
        "player_identity": player_identity,
        "player_special":  "",
        "heroines":        heroines,
        "tone":            tone,
        "main_plot":       "",
    }
    return _wz_show_summary(structured)


def run_new_game_wizard() -> "tuple[str, dict]":
    print("\n" + "═" * 55)
    print("  ★  新游戏设定向导")
    print("  各步骤支持：b=返回上一步  r=随机  q=取消向导")
    print("═" * 55)

    # 第0步：选择设定模式
    mode = _wz_mode_select()
    if mode is None:
        return "", {}

    if mode == "A":
        result = _wz_free_input()
        if result is None:
            return "", {}
        if result == _BACK:
            return run_new_game_wizard()
        return result

    # B模式：引导设定（原有流程）
    step_fns = [_wz_world, _wz_player, _wz_heroines, _wz_tone, _wz_plot]
    results: list = [None] * len(step_fns)
    si = 0
    while si < len(step_fns):
        res = step_fns[si]()
        if res is None: return "", {}
        if res == _BACK:
            si = max(0, si - 1); continue
        results[si] = res
        si += 1
    structured = {
        "world_bg":        results[0]["world_bg"],
        "player_name":     results[1]["player_name"],
        "player_identity": results[1]["player_identity"],
        "player_special":  results[1]["player_special"],
        "heroines":        results[2]["heroines"],
        "tone":            results[3]["tone"],
        "main_plot":       results[4]["main_plot"],
    }
    result = _wz_show_summary(structured)
    if result is None:
        return "", {}
    if result == _BACK:
        return run_new_game_wizard()
    return result


# ══════════════════════════════════════════════════════════════════
# 首回合（向导结果 → GM）
# ══════════════════════════════════════════════════════════════════

def send_first_turn(state: dict, instruction: str, structured: "dict | None" = None) -> None:
    """将开局指令作为第一回合发给 GM。世界设定注入 system prompt 确保优先级。
    structured: run_new_game_wizard() 返回的结构化设定，用于初始化 world_state 字段。
    """
    state["round"] += 1
    logger.info("首回合开始，指令长度 %d 字", len(instruction))
    system    = build_system_prompt({}, initial_setting=instruction)
    streaming = is_streaming()
    if streaming:
        print(f"\n  [GM构建世界中...]\n", end="", flush=True)
    else:
        print("\n  [GM构建世界中...]")
    try:
        response = generate(system, build_user_prompt("请按照系统设定直接开始第一个场景。"))
        logger.info("首回合响应完成，响应长度 %d 字", len(response))
        narrative, updates = parse_response(response)
        if not streaming:
            print_sep()
            print_response(narrative)
        print_sep()
        state["world_state"]["_initial_setting"] = instruction  # 持久化首回合设定，供第1-9回合 system prompt 使用
        # 将向导结构化设定写入 world_state，防止 AI 用模板占位符填充存档
        if structured:
            state["world_state"]["world"] = {
                "background":      structured.get("world_bg", ""),
                "player_name":     structured.get("player_name", ""),
                "player_identity": structured.get("player_identity", ""),
                "tone":            structured.get("tone", ""),
                "player_scope":    "",
            }
            state["world_state"]["player"] = {
                "name":     structured.get("player_name", ""),
                "identity": structured.get("player_identity", ""),
                "special":  structured.get("player_special", ""),
            }
            if "heroines" not in state["world_state"]:
                state["world_state"]["heroines"] = []
            logger.info("world_state 已初始化：player=%s  tone=%s",
                        structured.get("player_name"), structured.get("tone"))
        apply_updates(state["world_state"], updates)
        state["history"].append({"role": "user",      "content": instruction})
        state["history"].append({"role": "assistant", "content": narrative})
        _trim_history(state)
    except Exception as e:
        logger.exception("首回合 LLM 调用失败")
        err = str(e)
        if len(err) > 300:
            err = err[:300] + " …[已截断]"
        print(f"  错误：{err}")
        state["round"] -= 1
        print("  提示：请手动输入开局设定让GM开始游戏")
        print_sep()


# ══════════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    # ── 日志初始化（最优先，其他模块的 logger 依赖它）─────────────
    try:
        _cfg = load_config()
        _setup_logging(
            debug     = _cfg.get("debug", False),
            log_level = _cfg.get("log_level", "INFO"),
        )
        logger.info("=== 通用叙事游戏引擎 v4.0 启动 ===")
        logger.info(
            "已加载配置：provider=%s  stream=%s  debug=%s  log_level=%s",
            _cfg.get("active_provider"), _cfg.get("stream"),
            _cfg.get("debug"), _cfg.get("log_level", "INFO"),
        )
        logger.info("日志路径：%s", _LOG_FILE)
    except Exception:
        # config 读取失败也不能崩溃，使用默认值
        _setup_logging()
        logger.exception("config.json 读取失败，使用默认配置")

    os.system("cls" if os.name == "nt" else "clear")
    print("=" * 55)
    print("       通用叙事游戏引擎  v4.0")
    print("       输入 /help 查看命令")
    print("=" * 55)

    story_name, initial_save = choose_or_create_story()
    print(f"\n  故事：{story_name}")

    state = empty_state()

    if initial_save:
        state = restore_state(initial_save)
        print(
            f"  已读取存档（第 {state['round']} 回合，"
            f"会话历史 {len(state['history'])} 条）"
        )
        print_sep()
        # 显示上次 GM 回复，让玩家知道从哪里继续
        last_gm = next(
            (m["content"] for m in reversed(state["history"]) if m["role"] == "assistant"),
            None,
        )
        if last_gm:
            print("  ─── 上次存档场景 ────────────────────────")
            print(last_gm)
            print_sep()
        print("  ↑ 以上为上次存档内容，继续游戏请直接输入行动")
    else:
        print("  新故事")
        print_sep()
        wizard_result, wizard_structured = run_new_game_wizard()
        if wizard_result:
            send_first_turn(state, wizard_result, wizard_structured)
        else:
            print("  向导已跳过，请直接输入开局设定让GM开始游戏")
            print_sep()

    while True:
        try:
            user_input = input(f"\n[第{state['round']+1}回合] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  游戏中断")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd   = parts[0].lower()
            arg   = parts[1] if len(parts) > 1 else ""

            if cmd == "/exit":
                do_save(state, story_name, label="exit")
                print("  再见。"); break
            elif cmd == "/save":
                do_save(state, story_name, label=arg or "manual")
            elif cmd in ("/load", "/rollback"):
                do_load(story_name, state)
            elif cmd == "/delete":
                do_delete(story_name)
            elif cmd == "/status":
                print_status(state)
            elif cmd == "/new":
                confirm = input(
                    "  重新开始将清除当前未存档进度，确认？(y / 回车取消) > "
                ).strip()
                if confirm.lower() == "y":
                    state = empty_state()
                    print("  已重置，启动向导...")
                    print_sep()
                    wizard_result, wizard_structured = run_new_game_wizard()
                    if wizard_result:
                        send_first_turn(state, wizard_result, wizard_structured)
                    else:
                        print("  向导已取消，请手动输入开局设定")
                        print_sep()
            elif cmd == "/help":
                print_help()
            else:
                print(f"  未知命令：{cmd}，输入 /help 查看可用命令")
            continue

        # ── 正常回合 ──────────────────────────────────────────────
        state["round"] += 1
        logger.debug("回合 %d 开始，玩家输入：%s", state["round"], _trunc(user_input))
        system    = build_static_system_prompt(state.get("world_state", {}))
        streaming = is_streaming()

        if streaming:
            print(f"\n  [GM 第{state['round']}回合]", flush=True)
        else:
            print("\n  [GM思考中...]\n")

        try:
            # 动态上下文（时间/事件/记忆等）注入 user message，不放 system prompt
            _dyn = build_dynamic_context(state.get("world_state", {}))
            _scene_anchor = _build_scene_anchor(state["history"])
            # GM控制台模式：去掉"玩家："包装，直接标记为导演指令
            _is_gm_cmd = user_input.lower().startswith("gm ")
            if _is_gm_cmd:
                _gm_content = user_input[3:].strip()
                _user_msg = (_dyn + "\n" if _dyn else "") + (
                    f"【GM导演模式 · 暂停叙事】\n"
                    f"以下是GM的幕后讨论，不代表任何游戏内行为，禁止推进剧情，禁止生成叙事段落。\n"
                    f"请以GM视角直接回答讨论内容。\n\n"
                    f"GM：{_gm_content}"
                )
            else:
                _prefix = (_dyn + "\n" if _dyn else "") + (_scene_anchor + "\n" if _scene_anchor else "")
                _user_msg = (
                    _prefix
                    + "\n[玩家指令]\n" + user_input + "\n[/玩家指令]\n\n"
                    + "请严格根据上述玩家指令推进叙事，不得替换为其他行为。"
                ) if _prefix else (
                    "[玩家指令]\n" + user_input + "\n[/玩家指令]\n\n"
                    + "请严格根据上述玩家指令推进叙事，不得替换为其他行为。"
                )
            # 使用短窗口历史 + 当前输入（禁止拼接完整历史）
            response = generate_with_history(system, state["history"], _user_msg)
            logger.info("回合 %d 完成，响应 %d 字", state["round"], len(response))
        except Exception as e:
            logger.exception("回合 %d LLM 调用失败", state["round"])
            err = str(e)
            if len(err) > 300:
                err = err[:300] + " …[已截断]"
            print(f"  错误：{err}")
            state["round"] -= 1
            continue

        narrative, updates = parse_response(response)

        if not streaming:
            print_sep()
            print_response(narrative)
        print_sep()

        apply_updates(state.get("world_state", {}), updates)

        # history 存紧凑格式（只含场景锚点+玩家指令，不含动态上下文前缀）
        # 动态上下文每回合由 build_dynamic_context() 重新注入当前消息，history 不需重复存储
        # 这可避免每条 user 消息膨胀至 ~2500 字，让 _HISTORY_CHAR_BUDGET 能保留更多轮
        if _is_gm_cmd:
            _compact_user = _user_msg  # GM 导演模式保持原样（无动态上下文前缀）
        else:
            _compact_user = (
                (_scene_anchor + "\n" if _scene_anchor else "")
                + "[玩家指令]\n" + user_input + "\n[/玩家指令]"
            )
        state["history"].append({"role": "user",      "content": _compact_user})
        state["history"].append({"role": "assistant", "content": narrative})
        _trim_history(state)   # 保持短窗口，长期记忆靠 event_cards

        if state["round"] % AUTO_SAVE_EVERY == 0:
            logger.info("自动存档触发，回合 %d", state["round"])
            print(f"\n  [第{state['round']}回合——自动存档]")
            do_save(state, story_name, label="auto")


_validate_field_registry()

if __name__ == "__main__":
    main()
