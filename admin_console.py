"""
admin_console.py — 管理员控制台（直接读写存档 JSON）

用法：
    python admin_console.py --save <存档路径> <命令> [参数]

命令列表：
    list_npcs               列出所有NPC及当前状态
    show_npc   --name NAME  显示指定NPC完整档案
    set_affection   --name NAME --value N    设置指定NPC好感度 [0,100]
    set_all_affection       --value N        批量设置所有NPC好感度
    set_world_tone  --tone TONE              修改 world_config.narrative_tone
    set_weight      --name NAME --value N    修改指定heroine出场权重 [1,20]
    add_knowledge   --name NAME --text TEXT  向NPC认知档案追加一条（自动去重）
    add_milestone   --name NAME --event EVT --detail DETAIL
                                             向NPC关系节点追加一条
    promote_npc     --name NAME              将supporting升格为heroine
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ──────────────────────────────────────────────────────────────────
# 存档读写
# ──────────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        sys.exit(f"[错误] 存档文件不存在：{path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"[错误] 存档 JSON 解析失败：{e}")


def _save(data: dict, path: str) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[已保存] {path}")


# ──────────────────────────────────────────────────────────────────
# 角色查找工具
# ──────────────────────────────────────────────────────────────────

def _get_heroines(data: dict) -> list:
    """返回 heroines 列表（兼容新旧格式）。"""
    return (
        data.get("characters", {}).get("heroines", [])
        or data.get("heroines", [])
    )


def _get_supporting(data: dict) -> list:
    """返回 supporting_characters 列表（兼容新旧格式）。"""
    return (
        data.get("characters", {}).get("supporting_characters", [])
        or data.get("supporting_characters", [])
    )


def _find_npc(data: dict, name: str) -> "tuple[dict | None, str]":
    """
    在 heroines 和 supporting_characters 中查找 NPC。
    返回 (npc_dict, category)，category 为 'heroine' 或 'supporting'。
    找不到返回 (None, '')。
    """
    for h in _get_heroines(data):
        if isinstance(h, dict) and h.get("name") == name:
            return h, "heroine"
    for s in _get_supporting(data):
        if isinstance(s, dict) and s.get("name") == name:
            return s, "supporting"
    return None, ""


def _all_npcs(data: dict) -> list:
    """返回所有 NPC（heroines + supporting_characters）。"""
    return _get_heroines(data) + _get_supporting(data)


# ──────────────────────────────────────────────────────────────────
# 命令实现
# ──────────────────────────────────────────────────────────────────

def cmd_set_affection(data: dict, name: str, value: int) -> None:
    """将指定 NPC 的 affection 设为 value，裁剪到 [0, 100]。"""
    npc, category = _find_npc(data, name)
    if npc is None:
        sys.exit(f"[错误] 找不到NPC：{name}")
    clamped = max(0, min(100, value))
    npc["affection"] = clamped
    print(f"[OK] {name}（{category}）affection → {clamped}")


def cmd_set_all_affection(data: dict, value: int) -> None:
    """将所有 NPC 的 affection 统一设为 value，裁剪到 [0, 100]。"""
    clamped = max(0, min(100, value))
    count = 0
    for npc in _all_npcs(data):
        if isinstance(npc, dict):
            npc["affection"] = clamped
            count += 1
    print(f"[OK] 已将 {count} 个NPC的 affection 统一设为 {clamped}")


def cmd_set_world_tone(data: dict, tone: str) -> None:
    """修改 world_config.narrative_tone。"""
    # 兼容新旧格式
    wc = (
        data.get("world", {}).get("world_config")
        or data.get("world_rules", {}).get("world_config")
    )
    if wc is None:
        # 若 world_config 不存在则创建
        data.setdefault("world", {}).setdefault("world_config", {})
        wc = data["world"]["world_config"]
    wc["narrative_tone"] = tone
    print(f"[OK] world_config.narrative_tone → {tone}")


def cmd_set_weight(data: dict, name: str, value: int) -> None:
    """修改指定 heroine 的 appearance_weight.value，裁剪到 [1, 20]。"""
    npc, category = _find_npc(data, name)
    if npc is None:
        sys.exit(f"[错误] 找不到NPC：{name}")
    if category != "heroine":
        print(f"[警告] {name} 是 supporting_character，不是 heroine，仍继续修改")
    clamped = max(1, min(20, value))
    aw = npc.setdefault("appearance_weight", {"value": 10, "consecutive_absent": 0})
    aw["value"] = clamped
    print(f"[OK] {name} appearance_weight.value → {clamped}")


def cmd_add_knowledge(data: dict, name: str, text: str) -> None:
    """向指定 NPC 的 player_knowledge 追加一条，自动去重。"""
    npc, _ = _find_npc(data, name)
    if npc is None:
        sys.exit(f"[错误] 找不到NPC：{name}")
    pk = npc.setdefault("player_knowledge", [])
    if text in pk:
        print(f"[跳过] 已存在该条目：{text}")
        return
    pk.append(text)
    print(f"[OK] {name} player_knowledge 新增：{text}")


def cmd_add_milestone(
    data: dict, name: str, event: str, detail: str
) -> None:
    """向指定 NPC 的 relationship_milestones 追加一条，自动填入存档日期和回合数。"""
    npc, _ = _find_npc(data, name)
    if npc is None:
        sys.exit(f"[错误] 找不到NPC：{name}")
    si = data.get("save_info", {})
    milestone = {
        "round":    si.get("turn", 0),
        "date":     si.get("date", "未知日期"),
        "location": si.get("location", "未知地点"),
        "event":    event,
        "detail":   detail,
    }
    npc.setdefault("relationship_milestones", []).append(milestone)
    print(f"[OK] {name} relationship_milestones 新增：{event} / {detail}")


def cmd_promote_npc(data: dict, name: str) -> None:
    """将 supporting_characters 中的 NPC 移入 heroines 列表，补全缺失字段。"""
    chars = data.get("characters", {})
    supporting = chars.get("supporting_characters") or data.get("supporting_characters", [])
    heroines   = chars.get("heroines")   or data.get("heroines", [])

    target = None
    for i, npc in enumerate(supporting):
        if isinstance(npc, dict) and npc.get("name") == name:
            target = supporting.pop(i)
            break

    if target is None:
        sys.exit(f"[错误] 在 supporting_characters 中找不到NPC：{name}")

    # 补全 heroine 必要字段
    target.setdefault("affection", target.get("affection", 50))
    target.setdefault("relationship_stage", "陌生人")
    target.setdefault("appearance_weight", {"value": 10, "consecutive_absent": 0})

    # 写回 heroines（兼容新旧格式）
    if "characters" in data and "heroines" in data["characters"]:
        data["characters"]["heroines"].append(target)
    elif "heroines" in data:
        data["heroines"].append(target)
    else:
        data.setdefault("characters", {}).setdefault("heroines", []).append(target)

    print(f"[OK] {name} 已从 supporting_characters 升格为 heroine")


def _promotion_suggested(npc: dict) -> bool:
    """判断 supporting NPC 是否建议升格。"""
    milestones = npc.get("relationship_milestones", [])
    # 条件1：关系节点 >= 3 条（proxy：3次以上实质互动）
    if len(milestones) >= 3:
        return True
    # 条件2：player_knowledge >= 3 条（proxy：玩家主动询问个人信息）
    if len(npc.get("player_knowledge", [])) >= 3:
        return True
    # 条件3：出现在关系节点中（排除仅有初识的情况，要求 >= 2 条）
    if len(milestones) >= 2:
        return True
    return False


def cmd_list_npcs(data: dict) -> None:
    """列出所有NPC的简要状态表。"""
    heroines   = _get_heroines(data)
    supporting = _get_supporting(data)

    print("【Heroines】")
    if heroines:
        for h in heroines:
            if not isinstance(h, dict):
                continue
            name    = h.get("name", "?")
            aff     = h.get("affection", 0)
            aw      = h.get("appearance_weight", {})
            weight  = aw.get("value", 10)
            absent  = aw.get("consecutive_absent", 0)
            print(f"  {name:<8} affection={aff:<4} weight={weight:<4} absent={absent}")
    else:
        print("  （无）")

    print("【Supporting】")
    if supporting:
        for s in supporting:
            if not isinstance(s, dict):
                continue
            name    = s.get("name", "?")
            aff     = s.get("affection", 0)
            ms_cnt  = len(s.get("relationship_milestones", []))
            hint    = "  [建议升格]" if _promotion_suggested(s) else ""
            print(f"  {name:<8} affection={aff:<4} 互动次数={ms_cnt}{hint}")
    else:
        print("  （无）")


def cmd_show_npc(data: dict, name: str) -> None:
    """输出指定NPC的完整档案。"""
    npc, category = _find_npc(data, name)
    if npc is None:
        sys.exit(f"[错误] 找不到NPC：{name}")

    print(f"─── {name}（{category}）───")
    print(f"affection       : {npc.get('affection', '未设置')}")
    print(f"relationship_stage: {npc.get('relationship_stage', '未设置')}")

    aw = npc.get("appearance_weight")
    if aw:
        print(f"appearance_weight : value={aw.get('value', 10)}  "
              f"consecutive_absent={aw.get('consecutive_absent', 0)}")
    else:
        print("appearance_weight : 未设置")

    pk = npc.get("player_knowledge", [])
    print(f"\nplayer_knowledge（{len(pk)} 条）：")
    for item in pk:
        print(f"  · {item}")

    ms = npc.get("relationship_milestones", [])
    print(f"\nrelationship_milestones（{len(ms)} 条）：")
    for m in ms:
        if isinstance(m, dict):
            print(f"  [{m.get('round', '?')}回合] {m.get('date', '')} "
                  f"@ {m.get('location', '')}  "
                  f"{m.get('event', '')}：{m.get('detail', '')}")
        else:
            print(f"  · {m}")

    # 其他字段（排除上面已展示的）
    shown = {"name", "affection", "relationship_stage",
             "appearance_weight", "player_knowledge", "relationship_milestones"}
    extras = {k: v for k, v in npc.items() if k not in shown}
    if extras:
        print("\n其他字段：")
        for k, v in extras.items():
            print(f"  {k}: {v}")


# ──────────────────────────────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────────────────────────────

COMMANDS = {
    "set_affection",
    "set_all_affection",
    "set_world_tone",
    "set_weight",
    "add_knowledge",
    "add_milestone",
    "promote_npc",
    "list_npcs",
    "show_npc",
    "set_narrative",
}

# narrative_config 各字段允许值
_NARRATIVE_ALLOWED: "dict[str, list[str]]" = {
    "pace":           ["slow", "moderate", "fast"],
    "tone":           ["warm", "neutral", "dark", "tense"],
    "style":          ["literary", "casual", "cinematic", "minimalist"],
    "pov":            ["second", "third"],
    "detail_level":   ["low", "medium", "high"],
    "dialogue_ratio": ["dialogue_heavy", "balanced", "narration_heavy"],
}


def cmd_set_narrative(data: dict, field: str, value: str) -> None:
    """修改 world_config.narrative_config 中指定字段的值，校验允许值。"""
    if field not in _NARRATIVE_ALLOWED:
        valid_fields = ", ".join(sorted(_NARRATIVE_ALLOWED))
        sys.exit(f"[错误] 无效字段：{field}。有效字段：{valid_fields}")
    allowed = _NARRATIVE_ALLOWED[field]
    if value not in allowed:
        sys.exit(f"[错误] 字段 {field} 不允许值 '{value}'。允许值：{', '.join(allowed)}")
    wc = (
        data.get("world", {}).get("world_config")
        or data.get("world_rules", {}).get("world_config")
    )
    if wc is None:
        data.setdefault("world", {}).setdefault("world_config", {})
        wc = data["world"]["world_config"]
    nc = wc.setdefault("narrative_config", {})
    nc[field] = value
    print(f"[OK] narrative_config.{field} → {value}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="admin_console.py",
        description="管理员控制台：直接读写存档 JSON",
    )
    p.add_argument("--save", required=True, metavar="PATH", help="存档文件路径")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    # set_affection
    sp = sub.add_parser("set_affection", help="设置指定NPC好感度")
    sp.add_argument("--name",  required=True)
    sp.add_argument("--value", required=True, type=int)

    # set_all_affection
    sp = sub.add_parser("set_all_affection", help="批量设置所有NPC好感度")
    sp.add_argument("--value", required=True, type=int)

    # set_world_tone
    sp = sub.add_parser("set_world_tone", help="修改 world_config.narrative_tone")
    sp.add_argument("--tone", required=True)

    # set_weight
    sp = sub.add_parser("set_weight", help="修改NPC出场权重")
    sp.add_argument("--name",  required=True)
    sp.add_argument("--value", required=True, type=int)

    # add_knowledge
    sp = sub.add_parser("add_knowledge", help="向NPC认知档案添加一条")
    sp.add_argument("--name", required=True)
    sp.add_argument("--text", required=True)

    # add_milestone
    sp = sub.add_parser("add_milestone", help="向NPC关系节点添加一条")
    sp.add_argument("--name",   required=True)
    sp.add_argument("--event",  required=True)
    sp.add_argument("--detail", required=True)

    # promote_npc
    sp = sub.add_parser("promote_npc", help="将supporting升格为heroine")
    sp.add_argument("--name", required=True)

    # list_npcs
    sub.add_parser("list_npcs", help="列出所有NPC及当前状态")

    # show_npc
    sp = sub.add_parser("show_npc", help="显示指定NPC完整档案")
    sp.add_argument("--name", required=True)

    # set_narrative
    sp = sub.add_parser("set_narrative", help="修改narrative_config字段值")
    sp.add_argument("--field", required=True,
                    choices=list(_NARRATIVE_ALLOWED),
                    metavar="FIELD")
    sp.add_argument("--value", required=True, metavar="VALUE")

    return p


def main(argv: "list[str] | None" = None) -> None:
    parser = build_parser()
    args   = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    data = _load(args.save)

    if   args.command == "set_affection":
        cmd_set_affection(data, args.name, args.value)
    elif args.command == "set_all_affection":
        cmd_set_all_affection(data, args.value)
    elif args.command == "set_world_tone":
        cmd_set_world_tone(data, args.tone)
    elif args.command == "set_weight":
        cmd_set_weight(data, args.name, args.value)
    elif args.command == "add_knowledge":
        cmd_add_knowledge(data, args.name, args.text)
    elif args.command == "add_milestone":
        cmd_add_milestone(data, args.name, args.event, args.detail)
    elif args.command == "promote_npc":
        cmd_promote_npc(data, args.name)
    elif args.command == "list_npcs":
        cmd_list_npcs(data)
        return          # 列表命令不保存
    elif args.command == "show_npc":
        cmd_show_npc(data, args.name)
        return          # 只读命令不保存
    elif args.command == "set_narrative":
        cmd_set_narrative(data, args.field, args.value)
    else:
        sys.exit(f"[错误] 未知命令：{args.command}")

    _save(data, args.save)


if __name__ == "__main__":
    main()
