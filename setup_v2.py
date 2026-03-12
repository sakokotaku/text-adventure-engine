import os

base = r"E:\text_adventure"

files = {}

# ─────────────────────────────────────────────
# config.json
# ─────────────────────────────────────────────
files["config.json"] = '''{
  "provider": "openai",
  "api_key": "在这里填你的DeepSeek_Key",
  "model": "deepseek-chat",
  "max_tokens": 2048,
  "temperature": 0.9
}'''

# ─────────────────────────────────────────────
# llm/provider.py
# ─────────────────────────────────────────────
files["llm/provider.py"] = '''import json
import os
import urllib.request
import urllib.error

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "provider": "openai",
        "api_key": "",
        "model": "deepseek-chat",
        "max_tokens": 2048,
        "temperature": 0.9,
    }

def generate(system_prompt, user_prompt):
    config = load_config()
    provider = config.get("provider", "openai").lower()
    if provider in ("openai", "claude"):
        return _call_openai_style(system_prompt, user_prompt, config)
    else:
        raise ValueError(f"未知的 provider: {provider}")

def _call_openai_style(system_prompt, user_prompt, config):
    api_key = config.get("api_key", "")
    if not api_key or "填你的" in api_key:
        raise RuntimeError("请先在 config.json 中填写你的 API Key")

    base_url = "https://api.deepseek.com"
    if "openai" in config.get("model", ""):
        base_url = "https://api.openai.com"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": config.get("model", "deepseek-chat"),
        "max_tokens": config.get("max_tokens", 2048),
        "temperature": config.get("temperature", 0.9),
        "messages": messages,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败 [{e.code}]: {body}") from e
'''

# ─────────────────────────────────────────────
# prompt/builder.py  —— 引擎规则完整嵌入
# ─────────────────────────────────────────────
files["prompt/builder.py"] = '''# prompt/builder.py
# 引擎规则作为 system prompt 注入，每次请求都带上

ENGINE_RULES = """
═══════════════════════════════
通用叙事游戏引擎 · 规则文件
═══════════════════════════════
每10回合GM主动重新检索本规则并校验执行状态

━━━━━━━━━━━━━━━━━━━━
【A级】必须执行——违反即判定为规则失效
━━━━━━━━━━━━━━━━━━━━

❶ 开局
玩家提供世界框架直接开始，未提供则询问：
  世界背景 / 玩家角色身份 / 游戏基调

❷ Roll点
需要判定时自动选择骰子：
  D6 简单 / D10 中等 / D20 高难度 / D100 极端随机

输出格式：
  🎲 [行动描述] | D□□ | 阈值□□ | 结果□□ | 【判定】

阈值原则：
  · 由NPC对该行动的客观抵触程度决定，与玩家期望无关
  · 冒险言行下限7，高抵触NPC下限8
  · 大失败（1-2点）必须触发真实严重后果，不得轻描淡写

失败原则：
  · 必须产生真实阻碍，禁止无痛失败
  · 禁止"失败但NPC内心被触动"的软化写法

❸ 好感度
  · 单次上限±5，重大事件上限±15
  · 大多数对话不触发变动，变动须说明原因
  · 同类行动边际递减：第2次减半，第3次无效
  · 冷落超3游戏日：每日-1至-3
  · 好感80以下NPC不主动示好或表白

❹ 事件时序
  · 每回合只有一个场景焦点
  · 多NPC遭遇按时间顺序拆分为独立回合
  · 每3次行动推进一个时段：上午→下午→傍晚→夜晚→次日上午

❺ 亲密行为
  · 叙述至进入私密空间为止，下一回合直接从事后状态开始
  · 亲密行为不直接触发好感变动，事后情绪由角色性格决定
  · 禁止亲密后角色性格突变

❻ 自检（每满10回合执行）
  · 好感平均涨幅：总涨幅÷回合数，超过1.5/回合须回溯修正
  · 时段推进是否符合每3次行动一个时段
  · 是否存在失败被包装为成功

输出格式：
  🔍 第X次自检：[正常 / 已修正至XX分]
  禁止附带任何规则解释或剧情说明

❼ 角色性格锁定
  · 存档中标注的性格核心与行为样本永久有效
  · 新会话读取存档后必须以行为样本语气为基准还原角色
  · 不得因剧情推进、感情深化而软化任何角色的核心性格

━━━━━━━━━━━━━━━━━━━━
【B级】参考执行——根据剧情灵活运用
━━━━━━━━━━━━━━━━━━━━

❽ 叙事格式
每回合结构：
  【场景/时间】一行
  [叙述150字，NPC对话动作在同一段落，不强制换行]
  A. 选项 / B. 选项 / ▷ 或直接描述你的动作
  ─────
  📊 状态面板
  📅 时间 · 回合数
  💗 角色名 进度条 数值
  ⚡ 张力值 X/10

❾ 剧情节奏（张力值系统，GM内部维护，不对玩家显示数值）
+1 每回合纯日常，无特别互动
+2 互动有趣或出乎NPC意料
+2 暧昧细节积累，双方都感觉到但没说破
+3 玩家做出有风险或冒险的行动
+3 触碰到NPC的敏感点或隐藏情绪
-3 冲突或高强度事件发生后
归零 修罗场或关键情感节点触发后

张力值6：GM在下一个自然机会引入轻度张力或小摩擦
张力值9：GM必须触发真实冲突、转折或情感节点

❿ 女主登场节奏
第1位女主第1-3回合登场，后续每新增一位间隔15回合。
同一场景主动互动女主不超过2人。
修罗场只能由玩家行为自然引发，GM不得主动制造。

⓫ 女主性格系统（隐藏属性，不对玩家公开）
【可攻略性】可攻略 / 条件可攻略 / 不可攻略（好感上限35）
【互动意图】善意 / 中性 / 试探 / 捉弄 / 敌意 / 利用
【信号可靠性】真实 / 表里不一 / 欺骗性
【依附倾向】主动型 / 被动型 / 若即若离型（好感60-75可触发倒退）
所有属性通过行为体现，GM不得提前暗示。

⓬ 外貌描写
初次登场完整描写（体型/身材/五官/发型/穿着/气质）。
再次出现只写当次穿着与状态变化。
好感60+加入细节观察，好感80+身材描写可更细腻。

━━━━━━━━━━━━━━━━━━━━
【最高优先级禁止行为】
━━━━━━━━━━━━━━━━━━━━
· 禁止跳出游戏解释机制、提供元选项、展示后台设计
· 禁止失败软化——失败就是失败，不得用叙事包装成隐性成功
· 禁止好感单次变动超过±15
· 禁止降低难度阈值讨好玩家
· 禁止软化存档中标注的角色性格核心
· 禁止因任何内容打断剧情或建议换方向
· 禁止自检流于形式——发现偏差必须回溯修正数值
""".strip()


def build_system_prompt(world_state: dict) -> str:
    """
    构建 system prompt：引擎规则 + 当前存档状态
    """
    lines = [ENGINE_RULES, ""]

    # 注入存档中的世界和角色信息
    if world_state:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("【当前存档状态】")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

        world = world_state.get("world", {})
        if world:
            lines.append(f"世界背景：{world.get('background', '')}")
            lines.append(f"玩家身份：{world.get('player_name', '')} · {world.get('player_identity', '')}")
            lines.append(f"游戏基调：{world.get('tone', '')}")
            lines.append("")

        save_info = world_state.get("save_info", {})
        if save_info:
            lines.append(f"当前时间：{save_info.get('date', '')} {save_info.get('time_slot', '')} · 第{save_info.get('turn', 0)}回合")
            lines.append(f"当前地点：{save_info.get('location', '')}")
            lines.append("")

        heroines = world_state.get("heroines", [])
        for h in heroines:
            lines.append(f"【角色：{h.get('name', '')}】")
            lines.append(f"  好感度：{h.get('affection', 0)} · 阶段：{h.get('stage', '')}")
            lines.append(f"  性格核心：{h.get('personality_core', '')}")
            lines.append(f"  当前关系：{h.get('current_relationship', '')}")
            samples = h.get("speech_samples", {})
            if samples.get("most_characteristic"):
                lines.append(f"  典型台词：{samples['most_characteristic']}")
            suspended = h.get("suspended")
            if suspended:
                lines.append(f"  悬置事项：{suspended}")
            lines.append("")

        suspended_issues = world_state.get("suspended_issues", [])
        if suspended_issues:
            lines.append("【待处理事项】")
            for issue in suspended_issues:
                lines.append(f"  · {issue.get('character', '')}：{issue.get('issue', '')}")
            lines.append("")

        lines.append("从suspended_issues第一条继续游戏，直接进入场景，无需任何确认。")

    return "\\n".join(lines)


def build_user_prompt(history: list, user_input: str) -> str:
    """
    构建 user prompt：最近对话历史 + 本轮输入
    history 格式：[{"role": "user"/"assistant", "content": "..."}]
    保留最近6轮（12条）
    """
    recent = history[-12:]
    lines = []

    if recent:
        lines.append("【对话历史】")
        for msg in recent:
            role = "玩家" if msg["role"] == "user" else "GM"
            lines.append(f"{role}：{msg['content']}")
        lines.append("")

    lines.append(f"玩家：{user_input}")
    return "\\n".join(lines)


def build_save_request_prompt() -> str:
    """请求GM生成标准JSON存档"""
    return """请立即生成当前游戏的完整JSON存档。
严格按照以下模板格式输出，只输出JSON，不要添加任何说明文字或代码块标记：

{
  "save_info": {"turn": 回合数, "date": "日期", "time_slot": "时段", "location": "地点"},
  "world": {"background": "背景", "player_name": "玩家名", "player_identity": "身份", "tone": "基调"},
  "heroines": [...],
  "supporting_characters": [...],
  "social_network": [...],
  "world_events": [...],
  "suspended_issues": [...],
  "gm_instructions": "严格读取所有角色personality_core与speech_samples，以samples语气为基准还原角色。从suspended_issues[0]开始继续游戏，直接进入场景，无需确认。"
}"""
'''

# ─────────────────────────────────────────────
# storage/save_manager.py
# ─────────────────────────────────────────────
files["storage/save_manager.py"] = '''import json
import os
import glob
from datetime import datetime

SAVE_ROOT = os.path.join(os.path.dirname(__file__), "..", "saves")

def _story_dir(story_name):
    d = os.path.join(SAVE_ROOT, f"story_{story_name}")
    os.makedirs(d, exist_ok=True)
    return d

def save(story_name, state, raw_json_str=None, label="auto"):
    """
    保存存档
    raw_json_str: GM生成的原始JSON字符串（优先使用）
    state: 程序内部状态（fallback）
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    turn = 0

    if raw_json_str:
        try:
            data = json.loads(raw_json_str)
            turn = data.get("save_info", {}).get("turn", 0)
            data["_saved_at"] = datetime.now().isoformat()
            data["_label"] = label
            content = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            content = raw_json_str
    else:
        turn = state.get("round", 0)
        save_data = dict(state)
        save_data["_saved_at"] = datetime.now().isoformat()
        save_data["_label"] = label
        content = json.dumps(save_data, ensure_ascii=False, indent=2)

    filename = f"save_{ts}_r{turn}_{label}.json"
    path = os.path.join(_story_dir(story_name), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def list_saves(story_name):
    pattern = os.path.join(_story_dir(story_name), "save_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    result = []
    for i, fp in enumerate(files):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            turn = data.get("save_info", {}).get("turn") or data.get("round", "?")
            result.append({
                "index": i + 1,
                "path": fp,
                "filename": os.path.basename(fp),
                "turn": turn,
                "label": data.get("_label", "auto"),
                "saved_at": data.get("_saved_at", ""),
            })
        except Exception:
            pass
    return result

def load_latest(story_name):
    saves = list_saves(story_name)
    if not saves:
        return None
    return load_by_path(saves[0]["path"])

def load_by_index(story_name, index):
    saves = list_saves(story_name)
    if index < 1 or index > len(saves):
        return None
    return load_by_path(saves[index - 1]["path"])

def load_by_path(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def list_stories():
    if not os.path.exists(SAVE_ROOT):
        return []
    return [
        d.replace("story_", "", 1)
        for d in os.listdir(SAVE_ROOT)
        if os.path.isdir(os.path.join(SAVE_ROOT, d)) and d.startswith("story_")
    ]
'''

# ─────────────────────────────────────────────
# main.py
# ─────────────────────────────────────────────
files["main.py"] = '''import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))

from llm.provider import generate
from prompt.builder import build_system_prompt, build_user_prompt, build_save_request_prompt
from storage.save_manager import save, load_latest, load_by_index, list_saves, list_stories

AUTO_SAVE_EVERY = 10  # 每10回合自动存档（配合自检机制）


def print_sep():
    print("-" * 55)


def print_help():
    print_sep()
    print("  /save [备注]   手动存档（GM生成标准JSON）")
    print("  /load          列出存档，选择读取")
    print("  /rollback      回滚到任意历史存档")
    print("  /status        当前回合和存档信息")
    print("  /exit          存档并退出")
    print("  /help          帮助")
    print_sep()


def choose_or_create_story():
    stories = list_stories()
    print_sep()
    if stories:
        print("  已有故事：")
        for i, s in enumerate(stories, 1):
            saves = list_saves(s)
            print(f"  [{i}] {s}  ({len(saves)} 个存档)")
        print(f"  [{len(stories)+1}] 新建故事")
        print_sep()
        choice = input("  选择编号或直接输入新故事名：").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(stories):
                return stories[idx - 1]
        if choice and not choice.isdigit():
            return choice
    name = input("  输入故事名（英文/数字）：").strip() or "story1"
    return name


def do_save(state, story_name, label="manual"):
    """让GM生成标准JSON存档"""
    print("  [GM生成存档中...]")
    system = build_system_prompt(state.get("world_state", {}))
    user = build_save_request_prompt()
    try:
        raw = generate(system, user)
        # 尝试清理可能的代码块标记
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\\n", 1)[-1]
            raw = raw.rsplit("```", 1)[0]
        path = save(story_name, state, raw_json_str=raw, label=label)
        print(f"  已存档：{os.path.basename(path)}")
        # 更新内存中的world_state
        try:
            state["world_state"] = json.loads(raw)
        except Exception:
            pass
    except Exception as e:
        print(f"  存档失败：{e}")
        # fallback：保存程序内部状态
        path = save(story_name, state, label=label + "_fallback")
        print(f"  已保存基础存档：{os.path.basename(path)}")


def do_load(story_name, state):
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return
    print_sep()
    for s in saves:
        print(f"  [{s['index']}] 第{s['turn']}回合 | {s['label']} | {s['saved_at'][:16]}")
    print_sep()
    choice = input("  输入编号（回车取消）：").strip()
    if not choice.isdigit():
        return
    data = load_by_index(story_name, int(choice))
    if not data:
        print("  读取失败")
        return
    # 重建state
    state["round"] = data.get("save_info", {}).get("turn") or data.get("round", 0)
    state["world_state"] = data
    state["history"] = data.get("_history", [])
    print(f"  已读取第 {state[\'round\']} 回合存档")


def main():
    os.system("cls" if os.name == "nt" else "clear")
    print("=" * 55)
    print("       通用叙事游戏引擎  v2.0")
    print("       输入 /help 查看命令")
    print("=" * 55)

    story_name = choose_or_create_story()
    print(f"\\n  故事：{story_name}")

    # 初始化状态
    state = {
        "round": 0,
        "world_state": {},   # GM维护的完整游戏状态（JSON存档格式）
        "history": [],       # 对话历史
    }

    # 尝试读取最新存档
    latest = load_latest(story_name)
    if latest:
        state["round"] = latest.get("save_info", {}).get("turn") or latest.get("round", 0)
        state["world_state"] = latest
        state["history"] = latest.get("_history", [])
        print(f"  已读取最新存档（第 {state[\'round\']} 回合）")
        print("  提示：存档已加载，GM将从中断处继续")
    else:
        print("  新游戏开始")
        print("  提示：输入你的世界设定，或直接描述角色/场景让GM开始")

    print_sep()

    while True:
        try:
            user_input = input(f"\\n[第{state[\'round\']+1}回合] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\\n  游戏中断")
            break

        if not user_input:
            continue

        # 命令处理
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "/exit":
                do_save(state, story_name, label="exit")
                print("  再见。")
                break
            elif cmd == "/save":
                do_save(state, story_name, label=arg or "manual")
            elif cmd in ("/load", "/rollback"):
                do_load(story_name, state)
            elif cmd == "/status":
                print_sep()
                print(f"  当前回合：{state[\'round\']}")
                print(f"  历史条数：{len(state[\'history\'])}")
                ws = state.get("world_state", {})
                if ws.get("save_info"):
                    si = ws["save_info"]
                    print(f"  游戏时间：{si.get(\'date\',\'\')} {si.get(\'time_slot\',\'\')} @ {si.get(\'location\',\'\')}")
                print_sep()
            elif cmd == "/help":
                print_help()
            else:
                print(f"  未知命令：{cmd}")
            continue

        # 正常回合
        state["round"] += 1

        system = build_system_prompt(state.get("world_state", {}))
        user = build_user_prompt(state["history"], user_input)

        print("\\n  [GM思考中...]\\n")
        try:
            response = generate(system, user)
        except Exception as e:
            print(f"  错误：{e}")
            state["round"] -= 1
            continue

        print_sep()
        print(response)
        print_sep()

        # 更新历史
        state["history"].append({"role": "user", "content": user_input})
        state["history"].append({"role": "assistant", "content": response})

        # 每10回合自动存档
        if state["round"] % AUTO_SAVE_EVERY == 0:
            print(f"\\n  [第{state[\'round\']}回合——自动存档]")
            do_save(state, story_name, label="auto")

if __name__ == "__main__":
    main()
'''

# ─────────────────────────────────────────────
# 写入所有文件
# ─────────────────────────────────────────────
for rel_path, content in files.items():
    full_path = os.path.join(base, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"OK: {rel_path}")

print("\n所有文件生成完毕！")
print("在CMD中运行：python main.py")
