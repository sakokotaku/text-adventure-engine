#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复 gui.py 以适配新架构"""

# 读取原文件
with open('gui.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 替换 provider 导入
old_import = '''from llm.provider import (
    generate,
    is_streaming,
    load_config,
    get_provider_cfg,
)'''
new_import = '''from llm.provider import (
    generate,
    is_streaming,
    load_config,
    get_provider_cfg,
    get_context_config,
)'''
content = content.replace(old_import, new_import)

# 2. 替换 builder 导入
old_builder = '''from prompt.builder import (
    build_system_prompt,
    build_user_prompt,
    build_save_request_prompt,
)'''
new_builder = '''from prompt.builder import (
    build_system_prompt,
    build_user_prompt,
    build_save_request_prompt,
    build_summary_prompt,
)'''
content = content.replace(old_builder, new_builder)

# 3. 替换 restore_state 函数
old_restore = '''def restore_state(data: dict) -> dict:
    """从存档 dict 恢复 state。兼容新旧两种存档格式。"""
    state = empty_state()
    state["round"] = (
        data.get("save_info", {}).get("turn")
        or data.get("round", 0)
    )
    state["world_state"] = data
    state["history"] = data.get("_history", data.get("history", []))
    state["summary"] = data.get("_summary", "")
    return state'''

new_restore = '''def restore_state(data: dict) -> dict:
    """从存档 dict 恢复 state。"""
    state = empty_state()
    state["round"] = data.get("save_info", {}).get("turn", 0)
    state["world_state"] = data
    # 从存档中恢复 _history
    state["history"] = data.get("_history", [])
    state["summary"] = data.get("_summary", "")
    return state'''

content = content.replace(old_restore, new_restore)

# 4. 替换 maybe_summarize 函数
old_summarize = '''def maybe_summarize(state: dict, status_callback: Callable = None) -> None:
    """当 history 超过阈值时生成摘要。"""
    config = load_config()
    ctx = config.get("context", {})
    recent_turns: int = ctx.get("recent_turns", 6)
    threshold: int = ctx.get("summary_threshold", 20)'''

new_summarize = '''def maybe_summarize(state: dict, status_callback: Callable = None) -> None:
    """当 history 超过阈值时生成摘要。"""
    config = get_context_config()
    recent_turns: int = config.get("recent_turns", 6)
    threshold: int = config.get("summary_threshold", 20)'''

content = content.replace(old_summarize, new_summarize)

# 5. 替换 maybe_summarize 中的摘要生成逻辑
old_summary_gen = '''    hist_text = "\\n".join(
        f"{'玩家' if m['role'] == 'user' else 'GM'}：{m['content']}"
        for m in old_msgs
    )
    try:
        new_summary = generate(
            "你是故事摘要助手。用简洁中文（200字以内）总结以下对话中的"
            "关键事件、情感变化、角色关系变动。只输出摘要，不加任何说明。",
            hist_text,
        )'''

new_summary_gen = '''    try:
        summary_prompt = build_summary_prompt(old_msgs)
        new_summary = generate(
            "你是故事摘要助手。",
            summary_prompt,
        )'''

content = content.replace(old_summary_gen, new_summary_gen)

# 6. 替换 do_save 调用（GUI中是 self.do_save 方法）
old_save = '''            system = build_system_prompt(self.state.get("world_state", {}))
            user = build_save_request_prompt(history=self.state.get("history", []))'''

new_save = '''            config = get_context_config()
            recent_turns = config.get("recent_turns", 6)
            
            system = build_system_prompt(self.state.get("world_state", {}))
            user = build_save_request_prompt(history=self.state.get("history", []), recent_turns=recent_turns)'''

content = content.replace(old_save, new_save)

# 7. 替换 check_summarize 中的 maybe_summarize 调用
old_check = '''    def check_summarize(self):
        """检查是否需要生成摘要"""
        def status_callback(msg):
            self.set_status(msg)
        
        maybe_summarize(self.state, status_callback)'''

new_check = '''    def check_summarize(self):
        """检查是否需要生成摘要"""
        def status_callback(msg):
            self.set_status(msg)
        
        maybe_summarize(self.state, status_callback)'''

content = content.replace(old_check, new_check)

# 写入新文件
with open('gui.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('gui.py 已更新完成！')
print('主要修改：')
print('1. 添加了 get_context_config 和 build_summary_prompt 导入')
print('2. restore_state 现在正确从 _history 读取历史记录')
print('3. maybe_summarize 使用 get_context_config() 获取配置')
print('4. 摘要生成使用 build_summary_prompt')
print('5. do_save 调用 build_save_request_prompt 时传入 recent_turns')
