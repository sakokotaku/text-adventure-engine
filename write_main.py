# 用于写入完整 main.py 的辅助脚本

main_py_content = '''"""
main.py — 通用叙事游戏引擎 v3.0
运行方式：python main.py
"""

from __future__ import annotations

import os
import sys
import json
import random as _rng
import re as _re

sys.path.insert(0, os.path.dirname(__file__))

from llm.provider import (
    generate,
    is_streaming,
    load_config,
    get_provider_cfg,
    get_context_config,
)
from prompt.builder import (
    build_system_prompt,
    build_user_prompt,
    build_save_request_prompt,
    build_summary_prompt,
)
from storage.save_manager import (
    save,
    load_by_path,
    load_by_index,
    list_saves,
    list_stories,
    delete_save,
)

AUTO_SAVE_EVERY = 10

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


def print_sep():
    print("─" * 55)


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


def empty_state() -> dict:
    """返回一个干净的游戏状态。"""
    return {
        "round": 0,
        "world_state": {},
        "history": [],
        "summary": "",
    }


def restore_state(data: dict) -> dict:
    """从存档 dict 恢复 state。"""
    state = empty_state()
    state["round"] = data.get("save_info", {}).get("turn", 0)
    state["world_state"] = data
    # 从存档中恢复 _history
    state["history"] = data.get("_history", [])
    state["summary"] = data.get("_summary", "")
    return state


def maybe_summarize(state: dict) -> None:
    """当 history 超过阈值时生成摘要。"""
    config = get_context_config()
    recent_turns: int = config.get("recent_turns", 6)
    threshold: int = config.get("summary_threshold", 20)

    history = state["history"]
    max_msgs = recent_turns * 2
    threshold_msgs = threshold * 2

    if len(history) <= threshold_msgs:
        return

    old_msgs = history[:-max_msgs]
    state["history"] = history[-max_msgs:]

    if not old_msgs:
        return

    print("  [历史过长，正在生成摘要...]")
    
    try:
        summary_prompt = build_summary_prompt(old_msgs)
        new_summary = generate(
            "你是故事摘要助手。",
            summary_prompt,
        )
        existing = state.get("summary", "")
        state["summary"] = (existing + "\\n" + new_summary).strip() if existing else new_summary
        print(f"  [已将 {len(old_msgs)} 条历史压缩为摘要]")
    except Exception as e:
        state["history"] = old_msgs + state["history"]
        print(f"  [摘要生成失败，保留原历史: {e}]")


def do_save(state: dict, story_name: str, label: str = "manual") -> None:
    """让 GM 生成标准 JSON 存档。"""
    print("  [GM生成存档中...]")
    
    config = get_context_config()
    recent_turns = config.get("recent_turns", 6)
    
    system = build_system_prompt(state.get("world_state", {}))
    user = build_save_request_prompt(history=state.get("history", []), recent_turns=recent_turns)
    
    try:
        raw = generate(system, user)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\\n", 1)[-1].rsplit("```", 1)[0].strip()
        path = save(story_name, state, raw_json_str=raw, label=label)
        print(f"  已存档：{path.name if hasattr(path, 'name') else os.path.basename(str(path))}")
        try:
            state["world_state"] = json.loads(raw)
        except Exception:
            pass
    except Exception as e:
        print(f"  存档失败：{e}")
        path = save(story_name, state, label=label + "_fallback")
        print(f"  已保存基础存档：{path.name if hasattr(path, 'name') else os.path.basename(str(path))}")


def do_load(story_name: str, state: dict) -> None:
    """列出存档，让用户选择回滚。"""
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return
    print_sep()
    for s in saves:
        print(f"  [{s['index']:>2}] 第{s['turn']:>3}回合 | {s['label']:<12} | {s['saved_at'][:16]}")
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
    print(f"  已回滚到第 {state['round']} 回合存档（历史 {len(state['history'])} 条）")


def do_delete(story_name: str) -> None:
    """列出存档，让用户选择删除。"""
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return
    print_sep()
    for s in saves:
        print(f"  [{s['index']:>2}] 第{s['turn']:>3}回合 | {s['label']:<12} | {s['saved_at'][:16]}")
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
    confirm = input(f"  确认删除：第{target['turn']}回合 / {target['label']} ？(y / 回车取消) > ").strip()
    if confirm.lower() != "y":
        return
    if delete_save(story_name, idx):
        print(f"  已删除：{target['filename']}")
    else:
        print("  删除失败")


def print_status(state: dict) -> None:
    ws = state.get("world_state", {})
    print_sep()
    print(f"  当前回合：第 {state['round']} 回合")
    print(f"  对话历史：{len(state['history'])} 条  |  摘要：{len(state['summary'])} 字")

    try:
        config = load_config()
        pname = config.get("active_provider", "?")
        pcfg = get_provider_cfg(config)
        print(f"  当前模型：{pname} / {pcfg.get('model', '?')}")
    except Exception:
        pass

    if ws.get("save_info"):
        si = ws["save_info"]
        print(f"  游戏时间：{si.get('date', '')} {si.get('time_slot', '')} @ {si.get('location', '')}")

    player = ws.get("player", {})
    world = ws.get("world", {})
    if player:
        print(f"  玩家身份：{player.get('name', '')} · {player.get('identity', '')}")
    elif world:
        print(f"  玩家身份：{world.get('player_name', '')} · {world.get('player_identity', '')}")
    if world.get("tone"):
        print(f"  游戏基调：{world.get('tone', '')}")

    heroines = ws.get("heroines", [])
    if heroines:
        print()
        print("  ─── 角色状态 ────────────────────────────")
        for h in heroines:
            name = h.get("name", "?")
            aff = h.get("affection", 0)
            stage = h.get("stage", "")
            bar = _affection_bar(aff)
            print(f"  {name:<6} {bar} {aff:>3}  {stage}")

    suspended = ws.get("suspended_issues", [])
    if suspended:
        print()
        print("  ─── 待处理事项 ──────────────────────────")
        for iss in suspended[:3]:
            char = iss.get("character", "")
            issue = iss.get("issue", "")
            print(f"  · {char}：{issue}")

    if state.get("summary"):
        print()
        print("  ─── 故事摘要 ────────────────────────────")
        preview = state["summary"][:100].replace("\\n", " ")
        print(f"  {preview}..." if len(state["summary"]) > 100 else f"  {preview}")

    print_sep()


def _build_save_entries() -> list:
    """构建所有故事的存档平铺列表。"""
    entries = []
    for story in sorted(list_stories()):
        for s in list_saves(story):
            entries.append({"story": story, "save": s})
    return entries


def _print_main_menu(entries: list) -> int:
    """打印平铺存档菜单。"""
    print_sep()
    if entries:
        for i, e in enumerate(entries, 1):
            s = e["save"]
            saved = s["saved_at"][:16].replace("T", " ") if s["saved_at"] else "─────────────"
            print(f"  [{i:>2}] {e['story']:<12}· 第{s['turn']:>3}回合  {s['label']:<14}{saved}")
    else:
        print("  （暂无存档，请新建故事）")
    new_idx = len(entries) + 1
    print()
    print(f"  [{new_idx:>2}] 新建故事")
    print(f"  [ 0] 退出")
    if entries:
        print(f"  提示：d<编号> 删除该存档，如 d1")
    print_sep()
    return new_idx


def _input_story_name() -> str:
    """引导用户输入合法的故事名。"""
    while True:
        name = input("  新故事名（字母/数字/中文，回车默认 story1）：").strip()
        if not name:
            return "story1"
        if name.startswith("/"):
            print("  故事名不能以 / 开头")
            continue
        if any(c in name for c in r'\\\\/:*?"<>|'):
            print("  故事名含非法字符，请重新输入")
            continue
        return name


def choose_or_create_story() -> tuple:
    """显示平铺存档列表，让用户选择继续某个存档，或新建故事。"""
    while True:
        entries = _build_save_entries()
        new_idx = _print_main_menu(entries)

        choice = input("  输入编号：").strip()

        if not choice:
            continue

        if choice == "0":
            print("  再见。")
            sys.exit(0)

        if choice.lower() == "/help":
            print_sep()
            print("  主菜单操作：")
            print("  <编号>        读取该存档继续游戏")
            print(f"  {new_idx}（或最大编号） 新建故事")
            print("  d<编号>       删除该存档，如 d1")
            print("  0             退出程序")
            print_sep()
            continue

        if choice.startswith("/"):
            print("  游戏内命令请进入故事后使用")
            continue

        if choice.lower().startswith("d") and choice[1:].isdigit():
            didx = int(choice[1:])
            if 1 <= didx <= len(entries):
                e = entries[didx - 1]
                s = e["save"]
                confirm = input(f"  确认删除【{e['story']}】第{s['turn']}回合？(y / 回车取消) > ").strip()
                if confirm.lower() == "y":
                    if delete_save(e["story"], s["index"]):
                        print(f"  已删除：{s['filename']}")
                    else:
                        print("  删除失败")
            else:
                print(f"  编号无效" if entries else "  暂无存档")
            continue

        if not choice.isdigit():
            print(f"  请输入 0-{new_idx} 之间的数字")
            continue

        idx = int(choice)

        if 1 <= idx <= len(entries):
            e = entries[idx - 1]
            data = load_by_path(e["save"]["path"])
            if data is None:
                print("  读取存档失败，文件可能已损坏")
                continue
            return e["story"], data

        if idx == new_idx:
            return _input_story_name(), None

        print(f"  编号无效，请输入 0-{new_idx}")


_BACK = "__BACK__"

_TONE_CONFLICTS = [
    (frozenset({"轻松治愈", "暗黑虐恋"}), "「轻松治愈」与「暗黑虐恋」基调相反"),
    (frozenset({"轻松治愈", "悬疑剧情"}), "「轻松治愈」与「悬疑剧情」组合较跳跃"),
]


def _cancel(v: str) -> bool:
    return v.lower() == "q"


def _is_back(v: str) -> bool:
    return v.lower() in ("b", "back", "返回", "上一步")


def _is_rand(v: str) -> bool:
    return v.lower() in ("r", "随机")


def _parse_tone(raw: str):
    if _is_rand(raw):
        return _rng.choice(_TONE_PRESETS), ""

    parts = [p.strip() for p in _re.split(r"[+＋]", raw) if p.strip()]
    resolved = []
    extra = []
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


def _wz_world():
    print("\\n  【①】世界背景")
    print("  " + "─" * 38)
    for i, (name, desc) in enumerate(_WORLD_PRESETS, 1):
        print(f"  [{i}] {name:<8}  {desc}")
    print("  [7] 完全自定义")
    print("  [r] 随机  |  [q] 取消向导")
    while True:
        raw = input("\\n  选择编号，或直接描述背景 [默认1] > ").strip()
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


def _wz_player():
    print("\\n  【②】玩家身份")
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
        "player_name": player_name,
        "player_identity": player_identity,
        "player_special": raw,
    }


def _wz_heroines():
    print("\\n  【③】女主角设定")
    print("  " + "─" * 38)
    print(f"  性格参考：{_PERSONALITY_HINTS}")
    print("  [b] 返回上一步  |  [r] 随机  |  [q] 取消向导")

    while True:
        raw = input("  女主角数量 [1-4，默认1 / r=随机] > ").strip()
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        if not raw:
            hcount = 1
            break
        if _is_rand(raw):
            hcount = _rng.randint(1, 3)
            print(f"  → 随机数量：{hcount}")
            break
        m = _re.search(r"[1-4]", raw)
        if m:
            hcount = int(m.group())
            break
        print("  请输入 1-4 之间的数字，r 随机")

    heroines = []
    hi = 0
    while hi < hcount:
        print(f"\\n  ─ 女主角 {hi + 1} ─")
        raw = input(f"  名字 [女主{hi + 1} / r=随机] > ").strip()
        if _cancel(raw): return None
        if _is_back(raw):
            if hi == 0:
                return _BACK
            hi -= 1
            heroines.pop()
            continue
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


def _wz_tone():
    print("\\n  【④】游戏基调")
    print("  " + "─" * 38)
    for i, t in enumerate(_TONE_PRESETS, 1):
        print(f"  [{i}] {t}")
    print("  [r] 随机  |  支持组合：如 3+角色可攻略 / 2+5")
    print("  [b] 返回上一步  |  [q] 取消向导")
    while True:
        raw = input("\\n  选择编号（或组合）[默认2] > ").strip()
        if not raw:
            raw = "2"
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        tone, warning = _parse_tone(raw)
        if warning:
            print(f"\\n  ⚠  {warning}")
            confirm = input("  回车确认此组合 / b=重新选择 > ").strip()
            if _is_back(confirm):
                continue
        return {"tone": tone}


def _wz_plot():
    print("\\n  【⑤】主线剧情（可选）")
    print("  " + "─" * 38)
    print("  [1] 随GM自由发挥")
    print("  [2] 有明确主线")
    print("  [r] 随机主线")
    print("  [b] 返回上一步  |  [q] 取消向导")
    while True:
        raw = input("\\n  选择 [默认1] > ").strip()
        if not raw or raw == "1":
            return {"main_plot": ""}
        if _cancel(raw): return None
        if _is_back(raw): return _BACK
        if _is_rand(raw):
            return {"main_plot": "随机"}
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
    """引导新游戏设置，返回发给 GM 的开局指令字符串。"""
    print("\\n" + "═" * 55)
    print("  ★  新游戏设定向导")
    print("  各步骤支持：b=返回上一步  r=随机  q=取消向导")
    print("═" * 55)

    step_fns = [_wz_world, _wz_player, _wz_heroines, _wz_tone, _wz_plot]
    results = [None] * len(step_fns)
    si = 0

    while si < len(step_fns):
        res = step_fns[si]()
        if res is None:
            return ""
        if res == _BACK:
            si = max(0