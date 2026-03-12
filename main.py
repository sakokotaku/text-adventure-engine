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
    build_save_request_prompt,
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


# ══════════════════════════════════════════════════════════════════
# UI 工具
# ══════════════════════════════════════════════════════════════════

def print_sep():
    print("─" * 55)


def print_response(text: str) -> None:
    """打印 GM 回复，非空行后插入空行改善可读性。"""
    for line in text.split("\n"):
        print(line)
        if line.strip():
            print()


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


def restore_state(data: dict) -> dict:
    """从存档 dict 恢复 state。"""
    state = empty_state()
    state["round"]       = data.get("save_info", {}).get("turn", 0)
    state["world_state"] = data
    # 恢复最近 SESSION_WINDOW 轮对话，为新会话提供少量上下文
    old_hist = data.get("_history", [])
    state["history"] = old_hist[-(SESSION_WINDOW * 2):]
    # 立刻裁剪：防止旧存档膨胀历史在第一回合前传给GM
    _trim_history(state)
    return state


_HISTORY_CHAR_BUDGET = 4000   # history 总字符上限（约 2000 token）


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

    # 3. 裁剪 story_state 数组大小
    story = new_data.get("story_state", {})
    if story.get("event_cards"):
        story["event_cards"] = story["event_cards"][-10:]
    if story.get("recent_memory"):
        story["recent_memory"] = story["recent_memory"][-5:]


# ══════════════════════════════════════════════════════════════════
# 存档 / 读档
# ══════════════════════════════════════════════════════════════════


def _build_scene_anchor(history: list, maxlen: int = 200) -> str:
    """从 history 中提取最后一条 GM 消息，构造场景锚点前缀。

    history 为空或无 assistant 消息时返回空字符串。
    GM 消息超过 maxlen 字时只取末尾 maxlen 字。
    """
    last_gm = next(
        (m["content"] for m in reversed(history) if m["role"] == "assistant"),
        None,
    )
    return f"【当前场景延续】{last_gm[-maxlen:]}\n" if last_gm else ""


def do_save(state: dict, story_name: str, label: str = "manual") -> None:
    """让 GM 生成三层 JSON 存档，失败时 fallback 到程序内部状态。"""
    print("  [GM生成存档中...]")
    logger.info("开始存档：story=%s  label=%s  回合=%d", story_name, label, state.get("round", 0))

    debug       = is_debug()
    system      = build_static_system_prompt(state.get("world_state", {}))
    _dyn        = build_dynamic_context(state.get("world_state", {}))
    save_prompt = build_save_request_prompt()
    _round_hint = f"当前回合数：{state.get('round', 0)}，save_info.turn必须填写此数字。\n"
    # 动态上下文 + 存档请求一起发给 GM。
    # 注意：存档生成不传 history——_dyn 已包含完整当前世界状态，
    # 传入对话历史会引入冗余内容（约 5000+ 字），增加延迟并干扰 JSON 结构。
    _save_user_msg = _round_hint + (_dyn + "\n" if _dyn else "") + save_prompt

    try:
        raw = generate(system, _save_user_msg, force_stream=False, max_tokens_override=4096)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        # 解析并执行写入锁定
        json_ok = False
        try:
            new_data = json.loads(raw)
            _enforce_locks(new_data, state.get("world_state", {}))
            raw      = json.dumps(new_data, ensure_ascii=False, indent=2)
            json_ok  = True
        except json.JSONDecodeError as je:
            # JSON 无效：提示但不崩溃，原样存为 txt（save() 会处理扩展名）
            logger.warning("存档 JSON 解析失败：%s", je)
            print(f"  [警告] 存档 JSON 解析失败：{je}（将以 txt 存储）")

        # debug 模式：打印原始 JSON 供检查
        if debug:
            print("\n  [DEBUG] 存档原始内容：")
            print(raw)
            print()

        path = save(story_name, state, raw_json_str=raw, label=label)
        fname = path.name if hasattr(path, "name") else os.path.basename(str(path))
        suffix = "（JSON）" if json_ok else "（txt，JSON 解析失败）"
        logger.info("存档完成%s：%s", suffix, fname)
        print(f"  已存档{suffix}：{fname}")

        # 更新内存中的 world_state
        if json_ok:
            try:
                state["world_state"] = json.loads(raw)
            except Exception:
                pass

    except Exception as e:
        # 截断超长异常正文（API 错误体可能很大），避免刷屏
        err = str(e)
        if len(err) > 300:
            err = err[:300] + " …[已截断]"
        logger.error("存档失败：%s", _trunc(str(e), 200))
        print(f"  存档失败：{err}")
        if debug:
            import traceback
            traceback.print_exc()
        path = save(story_name, state, label=label + "_fallback")
        fname = path.name if hasattr(path, "name") else os.path.basename(str(path))
        logger.info("已保存 fallback 基础存档：%s", fname)
        print(f"  已保存基础存档：{fname}")


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
    # ── 长期记忆：加载后检测 event_cards 是否超阈值，静默压缩 ──────
    if should_summarize(state.get("world_state", {})):
        logger.info("长期记忆：event_cards 达到阈值，开始压缩…")
        state["world_state"] = compress_events(
            state["world_state"],
            lambda sys_p, usr_p: generate(sys_p, usr_p, force_stream=False),
        )
        if state["world_state"].get("story_summary"):
            state["story_summary"] = state["world_state"]["story_summary"]
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
    print("  [7] 完全自定义")
    print("  [r] 随机  |  [q] 取消向导")
    while True:
        raw = input("\n  选择编号，或直接描述背景 [默认1] > ").strip()
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
            if 1 <= idx <= 6:
                n, d = _WORLD_PRESETS[idx - 1]
                return {"world_bg": f"{n}——{d}"}
            if idx == 7:
                while True:
                    desc = input("  自定义背景描述 [b=返回选项] > ").strip()
                    if _is_back(desc):
                        break
                    if _cancel(desc):
                        return None
                    if desc:
                        return {"world_bg": desc}
                    print("  请输入背景描述")
                continue
            print("  请输入 1-7，r，q，或直接描述背景")
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
    print(f"  性格参考：{_PERSONALITY_HINTS}")
    print("  [b] 返回上一步  |  [r] 随机  |  [q] 取消向导")
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


def run_new_game_wizard() -> str:
    print("\n" + "═" * 55)
    print("  ★  新游戏设定向导")
    print("  各步骤支持：b=返回上一步  r=随机  q=取消向导")
    print("═" * 55)
    step_fns = [_wz_world, _wz_player, _wz_heroines, _wz_tone, _wz_plot]
    results: list = [None] * len(step_fns)
    si = 0
    while si < len(step_fns):
        res = step_fns[si]()
        if res is None: return ""
        if res == _BACK:
            si = max(0, si - 1); continue
        results[si] = res
        si += 1
    world_bg        = results[0]["world_bg"]
    player_name     = results[1]["player_name"]
    player_identity = results[1]["player_identity"]
    player_special  = results[1]["player_special"]
    heroines        = results[2]["heroines"]
    tone            = results[3]["tone"]
    main_plot       = results[4]["main_plot"]
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
    instruction = "\n".join(lines)
    print("\n" + "═" * 55)
    print("  生成的开局指令预览：")
    print("─" * 55)
    print(instruction)
    print("═" * 55)
    confirm = input("  回车发送给GM / 输入内容替换 / b=重头重设 / q=取消\n  > ").strip()
    if _cancel(confirm): return ""
    if _is_back(confirm): return run_new_game_wizard()
    return confirm if confirm else instruction


# ══════════════════════════════════════════════════════════════════
# 首回合（向导结果 → GM）
# ══════════════════════════════════════════════════════════════════

def send_first_turn(state: dict, instruction: str) -> None:
    """将开局指令作为第一回合发给 GM。世界设定注入 system prompt 确保优先级。"""
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
        if not streaming:
            print_sep()
            print_response(response)
        print_sep()
        state["history"].append({"role": "user",      "content": instruction})
        state["history"].append({"role": "assistant", "content": response})
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
        # ── 长期记忆：启动时检测 event_cards 是否超阈值，静默压缩 ──
        if should_summarize(state.get("world_state", {})):
            logger.info("长期记忆：启动加载存档后检测到 event_cards 达到阈值，开始压缩…")
            state["world_state"] = compress_events(
                state["world_state"],
                lambda sys_p, usr_p: generate(sys_p, usr_p, force_stream=False),
            )
            if state["world_state"].get("story_summary"):
                state["story_summary"] = state["world_state"]["story_summary"]
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
        wizard_result = run_new_game_wizard()
        if wizard_result:
            send_first_turn(state, wizard_result)
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
                    wizard_result = run_new_game_wizard()
                    if wizard_result:
                        send_first_turn(state, wizard_result)
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
            _user_msg = (_scene_anchor + _dyn + "\n玩家：" + user_input) if (_scene_anchor or _dyn) else user_input
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

        if not streaming:
            print_sep()
            print_response(response)
        print_sep()

        state["history"].append({"role": "user",      "content": user_input})
        state["history"].append({"role": "assistant", "content": response})
        _trim_history(state)   # 保持短窗口，长期记忆靠 event_cards

        if state["round"] % AUTO_SAVE_EVERY == 0:
            logger.info("自动存档触发，回合 %d", state["round"])
            print(f"\n  [第{state['round']}回合——自动存档]")
            do_save(state, story_name, label="auto")


if __name__ == "__main__":
    main()
