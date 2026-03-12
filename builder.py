# prompt/builder.py
# 规则与存档模板从同目录的文本文件加载，修改规则直接编辑 txt/json 文件即可，无需改代码。

import logging
import os

from storage.memory import inject_summary_to_context

logger = logging.getLogger(__name__)

_PROMPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_text(filename: str, fallback: str = "") -> str:
    """从 prompt/ 目录加载文本文件，缺失时返回 fallback。"""
    path = os.path.join(_PROMPT_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("规则/模板文件缺失：%s（将使用空内容）", filename)
        return fallback


# ── 运行时加载：修改文件后重启引擎即生效 ─────────────────────────
ENGINE_RULES   = _load_text("engine_rules.txt")   # 规则文件
_SAVE_TEMPLATE = _load_text("save_template.json")  # 存档模板（v4 三层）

# system prompt 字数预算（超出时自动裁剪低优先级字段）
_SYSTEM_PROMPT_BUDGET        = 8000   # 字符数上限（中文约等于token数）
_HEROINE_SPEECH_TRIM_THRESHOLD = 5000  # 超过此值开始裁剪speech_samples

logger.debug(
    "builder 初始化：ENGINE_RULES=%d字  SAVE_TEMPLATE=%d字",
    len(ENGINE_RULES), len(_SAVE_TEMPLATE),
)


# ══════════════════════════════════════════════════════════════════
# 内部工具
# ══════════════════════════════════════════════════════════════════

def _is_new_format(world_state: dict) -> bool:
    """检测是否为 v4 三层格式（world_rules / characters / story_state）。"""
    return bool(
        world_state.get("world_rules")
        or world_state.get("characters")
        or world_state.get("story_state")
    )


def _render_heroine(lines: list, h: dict, trim: bool = False) -> None:
    """
    渲染单个女主角到 lines 列表（新旧格式通用）。

    静态字段（由程序硬锁定）：
      appearance / personality_core / speech_samples / hidden_attributes /
      address / first_reactions / private_anchors / nickname
    半静态字段（仅存档时由 AI 更新，会话内不变）：
      affection / relationship_stage / last_interaction
    旧格式兼容字段（v2/v3，可在确认无旧存档后移除）：
      stage / current_relationship / suspended
    """
    lines.append(f"【角色：{h.get('name', '')}】")

    if h.get("nickname"):
        lines.append(f"  外号：{h['nickname']}")

    # 好感 & 阶段（半静态：存档时更新，会话内稳定）
    affection = h.get("affection", 0)
    stage = h.get("relationship_stage") or h.get("stage", "")   # stage: v2/v3 兼容
    lines.append(f"  好感度：{affection} · 阶段：{stage}")
    lines.append(f"  性格核心：{h.get('personality_core', '')}")

    if h.get("appearance"):
        lines.append(f"  外貌：{h['appearance']}")

    # 称呼
    address = h.get("address", {})
    if address:
        pc = address.get("player_calls_her", "")
        sc = address.get("she_calls_player", "")
        if pc or sc:
            lines.append(f"  称呼：玩家叫她「{pc}」· 她叫玩家「{sc}」")

    # 隐藏属性
    hidden = h.get("hidden_attributes", {})
    if hidden:
        parts = []
        for key, label in (
            ("approachability", "可攻略性"),
            ("intent",          "意图"),
            ("signal_reliability", "信号"),
            ("attachment_style",   "依附"),
        ):
            if hidden.get(key):
                parts.append(f"{label}={hidden[key]}")
        if parts:
            lines.append(f"  隐藏属性：{'  '.join(parts)}")

    # 台词样本
    samples = h.get("speech_samples", {})
    if samples:
        # trim模式：只保留most_characteristic，丢弃daily/angry_or_hurt/exposed_or_flustered
        if trim:
            if samples.get("most_characteristic"):
                lines.append(f"  典型台词：{samples['most_characteristic']}")
        else:
            if samples.get("most_characteristic"):
                lines.append(f"  典型台词：{samples['most_characteristic']}")
            if samples.get("daily"):
                lines.append(f"  日常台词：{samples['daily']}")
            if samples.get("angry_or_hurt"):
                lines.append(f"  生气台词：{samples['angry_or_hurt']}")
            if samples.get("exposed_or_flustered"):
                lines.append(f"  被戳穿时：{samples['exposed_or_flustered']}")

    # 已建立的默契（必须视为既成事实）
    first_reactions = h.get("first_reactions", [])
    if first_reactions:
        lines.append("  已建立默契（不得重置为初见反应）：")
        for fr in first_reactions:
            lines.append(f"    · {fr}")

    # 私密共同记忆
    private_anchors = h.get("private_anchors", [])
    if private_anchors:
        lines.append("  仅两人知道的细节：")
        for pa in private_anchors:
            lines.append(f"    · {pa}")

    if h.get("last_interaction"):
        lines.append(f"  上次互动：{h['last_interaction']}")

    # ── 旧格式兼容字段（v2/v3，确认无旧存档后可移除）────────────
    if h.get("current_relationship"):                      # v2/v3: 当前关系描述
        lines.append(f"  当前关系：{h['current_relationship']}")
    if h.get("suspended"):                                  # v2/v3: 每角色悬置事项
        lines.append(f"  悬置事项：{h['suspended']}")

    lines.append("")


# ══════════════════════════════════════════════════════════════════
# ① 静态 System Prompt（会话期间不变，可安全缓存）
# ══════════════════════════════════════════════════════════════════

def build_static_system_prompt(world_state: dict, initial_setting: str = "") -> str:
    """
    构建静态 system prompt。

    包含（会话内不变）：
      · ENGINE_RULES
      · world_rules（不可变世界设定）
      · 玩家身份
      · characters（含 affection / stage 等半静态字段）

    不包含（每回合可变，见 build_dynamic_context）：
      · save_info（时间/回合/地点）
      · story_state.event_cards
      · story_state.recent_memory
      · story_state.suspended_issues
      · gm_instructions
      · npc_memory

    initial_setting: 向导生成的开局指令，仅第一回合使用。
    """
    logger.debug(
        "构建 static system prompt：initial_setting=%s  world_state_keys=%s",
        bool(initial_setting),
        list(world_state.keys()) if world_state else [],
    )
    lines = [ENGINE_RULES, ""]

    # ── 第一回合：把向导设定注入 system prompt ─────────────────────
    if initial_setting:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("【本局游戏世界设定（必须严格遵守，不得替换）】")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(initial_setting)
        lines.append("")
        result = "\n".join(lines)
        logger.debug("static system prompt（首回合设定）共 %d 字", len(result))
        return result

    if not world_state:
        result = "\n".join(lines)
        logger.debug("static system prompt（无存档）共 %d 字", len(result))
        return result

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("【当前存档状态】")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    new_fmt = _is_new_format(world_state)

    # ── ① World Rules（不可变设定）────────────────────────────────
    if new_fmt:
        wr = world_state.get("world_rules", {})
        if wr:
            lines.append("【世界规则（不可修改）】")
            if wr.get("setting"):
                lines.append(f"  世界背景：{wr['setting']}")
            if wr.get("tone"):
                lines.append(f"  游戏基调：{wr['tone']}")
            if wr.get("player_scope"):
                lines.append(f"  玩家权限：{wr['player_scope']}")
            lines.append("")
    else:
        # ── 旧格式兼容（v1/v2）：world 字段 ──────────────────────
        world = world_state.get("world", {})
        if world:
            lines.append(f"世界背景：{world.get('background', '')}")
            lines.append(f"游戏基调：{world.get('tone', '')}")
            lines.append("")

    # ── 玩家身份 ───────────────────────────────────────────────────
    if new_fmt:
        player = world_state.get("player", {})
        if player:
            pline = f"玩家：{player.get('name', '')} · {player.get('identity', '')}"
            if player.get("special"):
                pline += f"（{player['special']}）"
            lines.append(pline)
            lines.append("")
    else:
        # ── 旧格式兼容（v2/v3）────────────────────────────────────
        player = world_state.get("player", {})
        if player:
            pline = f"玩家：{player.get('name', '')} · {player.get('identity', '')}"
            if player.get("special"):
                pline += f"（{player['special']}）"
            lines.append(pline)
        else:
            # v1/v2：player_name / player_identity 挂在 world 下
            world = world_state.get("world", {})
            pname  = world.get("player_name", "")
            pident = world.get("player_identity", "")
            if pname or pident:
                lines.append(f"玩家：{pname} · {pident}")
        lines.append("")

    # ── ② Characters（稳定角色数据，含半静态 affection/stage）──────
    if new_fmt:
        chars      = world_state.get("characters", {})
        heroines   = chars.get("heroines", [])
        supporting = chars.get("supporting_characters", [])
    else:
        # ── 旧格式兼容（v2/v3）：顶层 heroines / supporting_characters
        heroines   = world_state.get("heroines", [])
        supporting = world_state.get("supporting_characters", [])

    # 预估当前长度，决定是否启用trim模式
    current_len = sum(len(l) for l in lines)
    trim_mode = current_len > _HEROINE_SPEECH_TRIM_THRESHOLD

    for h in heroines:
        _render_heroine(lines, h, trim=trim_mode)
        # 每渲染完一个女主重新评估
        current_len = sum(len(l) for l in lines)
        if current_len > _HEROINE_SPEECH_TRIM_THRESHOLD:
            trim_mode = True

    if supporting:
        lines.append("【配角】")
        for sc in supporting:
            sc_line = (
                f"  · {sc.get('name', '')}"
                f"（{sc.get('gender', '')}/{sc.get('type', '')}）"
                f"：{sc.get('relationship_to_player', '')}"
            )
            lines.append(sc_line)
            if sc.get("relationship_to_heroines"):
                lines.append(f"    与女主关联：{sc['relationship_to_heroines']}")
            if sc.get("current_status"):
                lines.append(f"    当前状态：{sc['current_status']}")
        lines.append("")

    result = "\n".join(lines)
    if len(result) > _SYSTEM_PROMPT_BUDGET:
        logger.warning(
            "static system prompt 超出预算：%d字 > %d字上限，建议减少角色字段",
            len(result), _SYSTEM_PROMPT_BUDGET,
        )
    logger.debug(
        "static system prompt 构建完成：格式=%s  共 %d 字",
        "v4新格式" if new_fmt else "旧格式兼容",
        len(result),
    )
    return result


# ══════════════════════════════════════════════════════════════════
# ② 动态上下文（每回合注入 user message，不进 system prompt）
# ══════════════════════════════════════════════════════════════════

def build_dynamic_context(world_state: dict) -> str:
    """
    构建动态上下文，随每轮 user message 发送。

    包含（每存档点更新）：
      · save_info：时间 / 回合数 / 地点
      · story_state.event_cards（最近5条）
      · story_state.recent_memory（最近5条）
      · story_state.suspended_issues
      · gm_instructions
      · npc_memory（旧格式兼容，v1/v2）

    返回空字符串表示无动态内容（如首回合、空存档）。
    """
    if not world_state:
        return ""

    lines: list[str] = []

    # ── 早期剧情摘要（长期记忆注入）────────────────────────────────
    summary_text = inject_summary_to_context(world_state)
    if summary_text:
        lines.append(summary_text)

    new_fmt = _is_new_format(world_state)

    # ── 时间 / 回合 / 地点 ─────────────────────────────────────────
    si = world_state.get("save_info", {})
    if si:
        lines.append(
            f"[第{si.get('turn', 0)}回合 · "
            f"{si.get('date', '')} {si.get('time_slot', '')} "
            f"@ {si.get('location', '')}]"
        )
        lines.append("")

    # ── Story State（动态记忆）──────────────────────────────────────
    if new_fmt:
        story         = world_state.get("story_state", {})
        event_cards   = story.get("event_cards", [])
        recent_memory = story.get("recent_memory", [])
        suspended     = story.get("suspended_issues", [])
    else:
        # ── 旧格式兼容（v1/v2）：event_log / suspended_issues 在顶层
        event_cards   = world_state.get("event_log", [])
        recent_memory = []
        suspended     = world_state.get("suspended_issues", [])

    # 关键事件卡（最近5条）
    if event_cards:
        lines.append("【关键事件记录】")
        for ec in event_cards[-5:]:
            if isinstance(ec, str):
                lines.append(f"  · {ec}")
            else:
                lines.append(f"  ▸ {ec.get('event', '')}")
                beats = ec.get("beats", [])
                if beats:
                    lines.append(f"    经过：{' → '.join(beats)}")
                er = ec.get("emotional_result", {})
                if er:
                    lines.append(
                        f"    情绪：玩家={er.get('player', '')} "
                        f"她={er.get('her', '')}"
                    )
                if ec.get("aftermath"):
                    lines.append(f"    余韵：{ec['aftermath']}")
        lines.append("")

    # 最近记忆（行为事实，最多5条）
    if recent_memory:
        lines.append("【最近记忆（行为事实，必须视为已发生）】")
        for rm in recent_memory[-5:]:
            lines.append(f"  · {rm}")
        lines.append("")

    # 待处理事项
    if suspended:
        lines.append("【待处理事项】")
        for issue in suspended:
            if isinstance(issue, str):
                lines.append(f"  · {issue}")
            else:
                lines.append(
                    f"  · {issue.get('character', '')}：{issue.get('issue', '')}"
                )
        lines.append("")

    # ── 旧格式兼容（v1/v2）：npc_memory ───────────────────────────
    npc_memory = world_state.get("npc_memory", {})
    if npc_memory:
        lines.append("【NPC记忆】")
        for npc, mem in npc_memory.items():
            lines.append(f"  {npc}：{mem}")
        lines.append("")

    # GM 指令（如有）
    gm_instructions = world_state.get("gm_instructions", "")
    if gm_instructions:
        lines.append("【GM指令】")
        lines.append(gm_instructions)
        lines.append("")

    # 继续游戏提示（仅在有待处理事项时追加）
    if suspended:
        lines.append("从suspended_issues第一条继续游戏，直接进入场景，无需任何确认。")

    result = "\n".join(lines)
    logger.debug("dynamic context 构建完成：共 %d 字", len(result))
    return result


# ══════════════════════════════════════════════════════════════════
# ③ User Prompt（只负责玩家输入格式化）
# ══════════════════════════════════════════════════════════════════

def build_user_prompt(user_input: str) -> str:
    """
    格式化玩家输入。
    动态上下文由调用方通过 build_dynamic_context() 拼接，不在此处理。
    对话历史通过 generate_with_history() 传入，不在此拼接。
    """
    return f"玩家：{user_input}"


# ══════════════════════════════════════════════════════════════════
# ④ 兼容入口（保留供旧调用点和测试使用）
# ══════════════════════════════════════════════════════════════════

def build_system_prompt(world_state: dict, initial_setting: str = "") -> str:
    """
    [兼容入口] 等价于 build_static_system_prompt()。

    历史上此函数同时包含动态内容（event_cards / recent_memory 等），
    现已拆分：动态部分由 build_dynamic_context() 负责。
    此入口保留供旧代码和测试使用，后续可废弃。
    """
    return build_static_system_prompt(world_state, initial_setting)


# ══════════════════════════════════════════════════════════════════
# ⑤ 存档请求 Prompt（v4 三层模板）
# ══════════════════════════════════════════════════════════════════

def build_save_request_prompt() -> str:
    """
    请求 GM 生成标准 JSON 存档（v4 三层格式）。
    调用方应将 build_dynamic_context() 的结果拼接在本 prompt 之前，
    确保 GM 获得完整的动态上下文。
    模板从 prompt/save_template.json 加载，修改模板直接编辑该文件。

    写入限制（由程序硬锁定，AI 修改无效）：
      · world_rules：整块锁定
      · characters[].appearance / personality_core / speech_samples /
        hidden_attributes / address / first_reactions / private_anchors / nickname：锁定
      · AI 只允许更新：affection / relationship_stage / last_interaction /
        story_state（event_cards / recent_memory / suspended_issues）
    """
    lines = [
        "请根据当前对话和游戏状态，生成完整JSON存档（v4格式）。",
        "只输出JSON，不要任何说明文字或代码块标记。",
        "注意：",
        "  · event_cards 最多保留最近10条，本次新增关键事件请追加在末尾",
        "  · recent_memory 只保留最近5条行为事实（一句话/条，禁止使用模糊描述如「关系升温」）",
        "  · 角色的 appearance/personality_core/speech_samples/hidden_attributes/address/first_reactions/private_anchors 字段原样保留，禁止修改",
        "",
    ]

    if _SAVE_TEMPLATE:
        lines.append(_SAVE_TEMPLATE)
    else:
        # fallback 极简模板
        lines.append("""{
  "save_info": {"turn": 0, "date": "", "time_slot": "", "location": ""},
  "world_rules": {"setting": "", "tone": "", "player_scope": ""},
  "characters": {"heroines": [], "supporting_characters": []},
  "story_state": {"time": "", "location": "", "event_cards": [], "recent_memory": [], "suspended_issues": []},
  "gm_instructions": ""
}""")

    result = "\n".join(lines)
    logger.debug("存档请求 prompt 构建完成：共 %d 字", len(result))
    return result
