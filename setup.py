import os

base = r"E:\text_adventure"

files = {}

files["config.json"] = '''{
  "provider": "claude",
  "api_key": "在这里填你的API_KEY",
  "model": "claude-opus-4-5",
  "max_tokens": 1024,
  "temperature": 0.9
}'''

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
        "provider": "claude",
        "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "model": "claude-opus-4-5",
        "max_tokens": 1024,
        "temperature": 0.9,
    }

def generate(prompt):
    config = load_config()
    provider = config.get("provider", "claude").lower()
    if provider == "claude":
        return _call_claude(prompt, config)
    elif provider in ("openai", "gpt"):
        return _call_openai(prompt, config)
    else:
        raise ValueError(f"未知的 provider: {provider}")

def _call_claude(prompt, config):
    api_key = config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "在这里填你的API_KEY":
        raise RuntimeError("请先在 config.json 中填写你的 API Key")
    payload = {
        "model": config.get("model", "claude-opus-4-5"),
        "max_tokens": config.get("max_tokens", 1024),
        "temperature": config.get("temperature", 0.9),
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    return _http_post("https://api.anthropic.com/v1/messages", payload, headers,
                      extractor=lambda d: d["content"][0]["text"])

def _call_openai(prompt, config):
    api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("请先在 config.json 中填写你的 API Key")
    payload = {
        "model": config.get("model", "gpt-4o"),
        "max_tokens": config.get("max_tokens", 1024),
        "temperature": config.get("temperature", 0.9),
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    return _http_post("https://api.openai.com/v1/chat/completions", payload, headers,
                      extractor=lambda d: d["choices"][0]["message"]["content"])

def _http_post(url, payload, headers, extractor):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return extractor(result).strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败 [{e.code}]: {body}") from e
'''

files["prompt/builder.py"] = '''RECENT_TURNS = 6

def build_prompt(world, summary, history, user_input):
    recent = history[-RECENT_TURNS * 2:]
    lines = []
    lines.append("=== 世界设定 ===")
    lines.append(world.strip())
    lines.append("")
    if summary.strip():
        lines.append("=== 剧情摘要 ===")
        lines.append(summary.strip())
        lines.append("")
    if recent:
        lines.append("=== 最近对话 ===")
        for msg in recent:
            role_label = "玩家" if msg["role"] == "user" else "GM"
            lines.append(f"{role_label}：{msg['content']}")
        lines.append("")
    lines.append("=== 当前输入 ===")
    lines.append(f"玩家：{user_input}")
    lines.append("")
    lines.append("请以 GM 身份继续推进故事。场景描写 150-200 字，提供 3 个可交互点，不解释选项含义。")
    return "\\n".join(lines)

def build_summary_prompt(old_summary, recent_history):
    lines = ["请将以下剧情内容压缩为简洁摘要（200字以内），保留关键人物、地点、已发生的重要事件："]
    lines.append("")
    if old_summary:
        lines.append(f"【现有摘要】{old_summary}")
        lines.append("")
    lines.append("【新增对话】")
    for msg in recent_history:
        role_label = "玩家" if msg["role"] == "user" else "GM"
        lines.append(f"{role_label}：{msg['content']}")
    lines.append("")
    lines.append("输出新的摘要，直接输出摘要文本，不要加标题或说明。")
    return "\\n".join(lines)
'''

files["storage/save_manager.py"] = '''import json
import os
import glob
from datetime import datetime

SAVE_ROOT = os.path.join(os.path.dirname(__file__), "..", "saves")

def _story_dir(story_name):
    d = os.path.join(SAVE_ROOT, f"story_{story_name}")
    os.makedirs(d, exist_ok=True)
    return d

def save(story_name, state, label="auto"):
    state["label"] = label
    state["saved_at"] = datetime.now().isoformat()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"save_{ts}_r{state.get('round', 0)}_{label}.json"
    path = os.path.join(_story_dir(story_name), filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return path

def list_saves(story_name):
    pattern = os.path.join(_story_dir(story_name), "save_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    result = []
    for i, fp in enumerate(files):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({
                "index": i + 1,
                "path": fp,
                "filename": os.path.basename(fp),
                "round": data.get("round", "?"),
                "label": data.get("label", "auto"),
                "saved_at": data.get("saved_at", ""),
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

files["main.py"] = '''import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from llm.provider import generate
from prompt.builder import build_prompt, build_summary_prompt
from storage.save_manager import save, load_latest, load_by_index, list_saves, list_stories

AUTO_SAVE_EVERY = 20

DEFAULT_WORLD = """这是一个架空的乱世江湖。朝廷式微，各方势力割据。
你是一个身份未明的旅人，携带一封无名信件，游走在江湖边缘。
世界规则：强者为尊，信息即权力，背叛是常态。
时代背景模糊，混杂着冷兵器与早期火器。"""

def print_sep():
    print("-" * 50)

def print_help():
    print_sep()
    print("  /save [备注]   手动存档")
    print("  /load          列出存档并选择读取")
    print("  /rollback      回滚到历史节点")
    print("  /summary       查看剧情摘要")
    print("  /status        查看当前状态")
    print("  /exit          保存并退出")
    print("  /help          显示帮助")
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
        choice = input("  选择编号，或直接输入新故事名：").strip()
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(stories):
                return stories[idx - 1]
        if choice and not choice.isdigit():
            return choice
    name = input("  输入故事名称（英文或数字）：").strip() or "default"
    return name

def do_save(state, story_name, label="manual"):
    path = save(story_name, dict(state), label=label)
    print(f"  已存档：{os.path.basename(path)}")

def do_load(story_name):
    saves = list_saves(story_name)
    if not saves:
        print("  没有可用存档")
        return None
    print_sep()
    for s in saves:
        print(f"  [{s[\'index\']}] 第{s[\'round\']}轮 | {s[\'label\']} | {s[\'saved_at\'][:16]}")
    print_sep()
    choice = input("  输入编号读取（直接回车取消）：").strip()
    if not choice.isdigit():
        return None
    state = load_by_index(story_name, int(choice))
    if state:
        print(f"  已读取第 {state.get(\'round\', \'?\')} 轮存档")
    else:
        print("  读取失败")
    return state

def compress_summary(state):
    print("  [自动压缩剧情摘要...]")
    compress_history = state["history"][:-6]
    if not compress_history:
        return
    prompt = build_summary_prompt(state["summary"], compress_history)
    try:
        new_summary = generate(prompt)
        state["summary"] = new_summary
        state["history"] = state["history"][-6:]
        print("  摘要已更新")
    except Exception as e:
        print(f"  摘要压缩失败（跳过）：{e}")

def main():
    os.system("cls")
    print("=" * 50)
    print("       AI 文字冒险  v0.1")
    print("       输入 /help 查看命令")
    print("=" * 50)

    story_name = choose_or_create_story()
    print(f"\\n  故事：{story_name}")

    state = load_latest(story_name)
    if state:
        print(f"  已读取最新存档（第 {state.get(\'round\', \'?\')} 轮）")
    else:
        print("  新游戏开始")
        state = {
            "round": 0,
            "world": DEFAULT_WORLD,
            "summary": "",
            "history": [],
        }

    print_sep()

    while True:
        try:
            user_input = input(f"\\n[第{state[\'round\']+1}轮] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\\n  游戏已中断")
            break

        if not user_input:
            continue

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
                new_state = do_load(story_name)
                if new_state:
                    state = new_state
            elif cmd == "/summary":
                print_sep()
                print(state["summary"] or "（暂无摘要）")
                print_sep()
            elif cmd == "/status":
                print_sep()
                print(f"  当前轮次：{state[\'round\']}")
                print(f"  历史记录：{len(state[\'history\'])} 条")
                print_sep()
            elif cmd == "/help":
                print_help()
            else:
                print(f"  未知命令：{cmd}")
            continue

        state["round"] += 1
        prompt = build_prompt(
            world=state["world"],
            summary=state["summary"],
            history=state["history"],
            user_input=user_input,
        )

        print("\\n  [GM 思考中...]\\n")
        try:
            response = generate(prompt)
        except Exception as e:
            print(f"  错误：{e}")
            state["round"] -= 1
            continue

        print_sep()
        print(response)
        print_sep()

        state["history"].append({"role": "user", "content": user_input})
        state["history"].append({"role": "assistant", "content": response})

        if state["round"] % AUTO_SAVE_EVERY == 0:
            compress_summary(state)
            path = save(story_name, dict(state), label="auto")
            print(f"  第 {state[\'round\']} 轮自动存档：{os.path.basename(path)}")

if __name__ == "__main__":
    main()
'''

# 写入所有文件
for rel_path, content in files.items():
    full_path = os.path.join(base, rel_path)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"OK: {rel_path}")

print("\n所有文件生成完毕！")
print("下一步：用记事本打开 E:\\text_adventure\\config.json 填写你的 API Key")
