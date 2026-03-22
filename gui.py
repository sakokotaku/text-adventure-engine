"""
gui.py — 通用叙事游戏引擎 GUI版本 v3.0
运行方式：python gui.py
使用 tkinter 构建图形界面
"""

from __future__ import annotations

import os
import sys
import json
import random as _rng
import re as _re
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(__file__))

from llm.provider import generate, generate_with_history, is_streaming, load_config, get_provider_cfg, get_context_config
from prompt.builder import (
    build_system_prompt,
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
from main import parse_response, apply_updates

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


# ══════════════════════════════════════════════════════════════════
# 游戏状态管理
# ══════════════════════════════════════════════════════════════════

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


def maybe_summarize(state: dict, status_callback: Callable = None) -> None:
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

    if status_callback:
        status_callback("正在生成历史摘要...")

    try:
        lines = ["请将以下对话历史压缩为简洁的故事摘要，保留关键事件和人物关系：\n"]
        for m in old_msgs:
            role = "玩家" if m["role"] == "user" else "GM"
            lines.append(f"{role}：{m['content']}")
        summary_prompt = "\n".join(lines)
        new_summary = generate(
            "你是故事摘要助手。",
            summary_prompt,
        )
        existing = state.get("summary", "")
        state["summary"] = (existing + "\n" + new_summary).strip() if existing else new_summary
        if status_callback:
            status_callback(f"已将 {len(old_msgs)} 条历史压缩为摘要")
    except Exception as e:
        state["history"] = old_msgs + state["history"]
        if status_callback:
            status_callback(f"摘要生成失败: {e}")


def _affection_bar(value, max_val=100, width=10):
    v = max(0, min(int(value or 0), max_val))
    filled = round(v / max_val * width)
    return "❤" * filled + "♡" * (width - filled)


# ══════════════════════════════════════════════════════════════════
# GUI 主应用
# ══════════════════════════════════════════════════════════════════

class GameApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("通用叙事游戏引擎 v3.0")
        self.root.geometry("1200x800")
        self.root.minsize(1000, 700)
        
        # 配置主题
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.configure_styles()
        
        # 游戏状态
        self.state = empty_state()
        self.story_name = ""
        self.current_frame: Optional[tk.Frame] = None
        
        # 创建主容器
        self.main_container = ttk.Frame(root)
        self.main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        self.status_bar = ttk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        
        # 显示主菜单
        self.show_main_menu()
    
    def configure_styles(self):
        """配置界面样式"""
        self.style.configure('Title.TLabel', font=('微软雅黑', 24, 'bold'))
        self.style.configure('Subtitle.TLabel', font=('微软雅黑', 12))
        self.style.configure('Card.TFrame', background='#f5f5f5')
        self.style.configure('Action.TButton', font=('微软雅黑', 11))
        self.style.configure('Menu.TButton', font=('微软雅黑', 12), padding=10)
        
        # 配置颜色
        self.root.configure(bg='#fafafa')
    
    def clear_frame(self):
        """清除当前框架"""
        if self.current_frame:
            self.current_frame.destroy()
    
    def set_status(self, message: str):
        """更新状态栏"""
        self.status_var.set(message)
        self.root.update_idletasks()
    
    # ══════════════════════════════════════════════════════════════════
    # 主菜单界面
    # ══════════════════════════════════════════════════════════════════
    
    def show_main_menu(self):
        """显示主菜单"""
        self.clear_frame()
        
        frame = ttk.Frame(self.main_container)
        frame.pack(fill=tk.BOTH, expand=True)
        self.current_frame = frame
        
        # 标题
        title = ttk.Label(frame, text="通用叙事游戏引擎", style='Title.TLabel')
        title.pack(pady=30)
        
        subtitle = ttk.Label(frame, text="v3.0 - 点击选择存档或新建故事", style='Subtitle.TLabel')
        subtitle.pack(pady=10)
        
        # 存档列表容器
        list_frame = ttk.LabelFrame(frame, text="继续游戏", padding=15)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=50, pady=20)
        
        # 创建存档列表
        self.create_save_list(list_frame)
        
        # 底部按钮
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=20)
        
        new_btn = ttk.Button(btn_frame, text="✨ 新建故事", command=self.show_new_game_wizard, style='Menu.TButton')
        new_btn.pack(side=tk.LEFT, padx=10)
        
        exit_btn = ttk.Button(btn_frame, text="退出", command=self.root.quit)
        exit_btn.pack(side=tk.LEFT, padx=10)
    
    def create_save_list(self, parent):
        """创建存档列表"""
        # 创建画布和滚动条
        canvas = tk.Canvas(parent, bg='#fafafa', highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        
        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw", width=parent.winfo_width()-30)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # 获取存档列表
        entries = []
        for story in sorted(list_stories()):
            for s in list_saves(story):
                entries.append({"story": story, "save": s})
        
        if not entries:
            empty_label = ttk.Label(scroll_frame, text="暂无存档，请点击下方「新建故事」开始游戏", 
                                   font=('微软雅黑', 11), foreground='gray')
            empty_label.pack(pady=50)
        else:
            for i, e in enumerate(entries):
                self.create_save_card(scroll_frame, e, i)
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 绑定鼠标滚轮
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)
    
    def create_save_card(self, parent, entry, index):
        """创建单个存档卡片"""
        story = entry["story"]
        s = entry["save"]
        
        card = ttk.Frame(parent, relief=tk.RIDGE, padding=10)
        card.pack(fill=tk.X, pady=5, padx=5)
        
        # 存档信息
        info_frame = ttk.Frame(card)
        info_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        story_label = ttk.Label(info_frame, text=story, font=('微软雅黑', 13, 'bold'))
        story_label.pack(anchor=tk.W)
        
        details = f"第{s['turn']}回合 | {s['label']} | {s['saved_at'][:16].replace('T', ' ')}"
        detail_label = ttk.Label(info_frame, text=details, font=('微软雅黑', 10), foreground='gray')
        detail_label.pack(anchor=tk.W)
        
        # 按钮
        btn_frame = ttk.Frame(card)
        btn_frame.pack(side=tk.RIGHT)
        
        load_btn = ttk.Button(btn_frame, text="读取", 
                             command=lambda: self.load_game(story, s))
        load_btn.pack(side=tk.LEFT, padx=2)
        
        delete_btn = ttk.Button(btn_frame, text="删除", 
                               command=lambda: self.delete_save_confirm(story, s, card))
        delete_btn.pack(side=tk.LEFT, padx=2)
    
    def delete_save_confirm(self, story: str, save_info: dict, card_widget):
        """确认删除存档"""
        if messagebox.askyesno("确认删除", 
            f"确定要删除【{story}】第{save_info['turn']}回合的存档吗？"):
            if delete_save(story, save_info['index']):
                card_widget.destroy()
                self.set_status(f"已删除存档: {save_info['filename']}")
            else:
                messagebox.showerror("错误", "删除失败")
    
    def load_game(self, story_name: str, save_info: dict):
        """加载游戏"""
        data = load_by_path(save_info["path"])
        if data is None:
            messagebox.showerror("错误", "读取存档失败，文件可能已损坏")
            return
        
        # 调试：打印读取的数据
        print(f"[DEBUG] 读取存档: {save_info['path']}")
        print(f"[DEBUG] data keys: {data.keys()}")
        print(f"[DEBUG] _history exists: {'_history' in data}")
        if '_history' in data:
            print(f"[DEBUG] _history length: {len(data['_history'])}")
        
        self.story_name = story_name
        self.state = restore_state(data)
        
        # 调试：打印恢复后的状态
        print(f"[DEBUG] restored state history length: {len(self.state.get('history', []))}")
        
        self.show_game_interface()
        
        # 如果历史记录最后一条是GM的回复，自动发送"继续"获取新内容
        history = self.state.get("history", [])
        if history and history[-1]["role"] == "assistant":
            self.set_status("GM继续故事中...")
            self.input_entry.config(state=tk.DISABLED)
            
            def continue_story():
                try:
                    ws = self.state.get("world_state", {})
                    system = build_system_prompt(ws)
                    dyn = build_dynamic_context(ws)
                    formatted = (dyn + "\n" if dyn else "") + build_user_prompt("继续")
                    self.state["history"].append({"role": "user", "content": formatted})
                    response = generate_with_history(
                        system,
                        self.state["history"][:-1],
                        formatted,
                    )
                    narrative, updates = parse_response(response)
                    apply_updates(self.state.setdefault("world_state", {}), updates)

                    self.root.after(0, lambda: self.add_gm_message(narrative))
                    self.root.after(0, lambda: self.set_status("就绪"))
                    self.root.after(0, lambda: self.input_entry.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.input_entry.focus())

                    self.root.after(100, self.check_summarize)

                except Exception as e:
                    self.root.after(0, lambda: self.add_system_message(f"继续故事失败: {e}"))
                    self.root.after(0, lambda: self.set_status("发生错误"))
                    self.root.after(0, lambda: self.input_entry.config(state=tk.NORMAL))
            
            threading.Thread(target=continue_story, daemon=True).start()
    
    # ══════════════════════════════════════════════════════════════════
    # 新游戏向导界面
    # ══════════════════════════════════════════════════════════════════
    
    def show_new_game_wizard(self):
        """显示新游戏向导"""
        self.clear_frame()
        
        frame = ttk.Frame(self.main_container)
        frame.pack(fill=tk.BOTH, expand=True)
        self.current_frame = frame
        
        # 标题
        title = ttk.Label(frame, text="✨ 新游戏设定向导", style='Title.TLabel')
        title.pack(pady=20)
        
        # 创建向导步骤框架
        self.wizard_data = {}
        self.wizard_step = 0
        self.wizard_steps = [
            ("世界背景", self.create_world_step),
            ("玩家身份", self.create_player_step),
            ("女主角设定", self.create_heroines_step),
            ("游戏基调", self.create_tone_step),
            ("主线剧情", self.create_plot_step),
        ]
        
        # 步骤内容区域
        self.wizard_content = ttk.Frame(frame)
        self.wizard_content.pack(fill=tk.BOTH, expand=True, padx=50, pady=10)
        
        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        progress = ttk.Progressbar(frame, variable=self.progress_var, maximum=100, length=400)
        progress.pack(pady=10)
        
        # 导航按钮
        nav_frame = ttk.Frame(frame)
        nav_frame.pack(pady=20)
        
        self.back_btn = ttk.Button(nav_frame, text="上一步", command=self.wizard_prev)
        self.back_btn.pack(side=tk.LEFT, padx=5)
        
        self.next_btn = ttk.Button(nav_frame, text="下一步", command=self.wizard_next)
        self.next_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(nav_frame, text="取消", command=self.show_main_menu).pack(side=tk.LEFT, padx=5)
        
        # 显示第一步
        self.show_wizard_step(0)
    
    def show_wizard_step(self, step: int):
        """显示指定步骤"""
        self.wizard_step = step
        self.progress_var.set((step / len(self.wizard_steps)) * 100)
        
        # 清除内容
        for widget in self.wizard_content.winfo_children():
            widget.destroy()
        
        # 更新按钮状态
        self.back_btn.config(state=tk.NORMAL if step > 0 else tk.DISABLED)
        self.next_btn.config(text="完成" if step == len(self.wizard_steps) - 1 else "下一步")
        
        # 显示步骤内容
        step_name, step_creator = self.wizard_steps[step]
        step_label = ttk.Label(self.wizard_content, text=f"步骤 {step + 1}/{len(self.wizard_steps)}: {step_name}",
                              font=('微软雅黑', 14, 'bold'))
        step_label.pack(pady=10)
        
        step_creator(self.wizard_content)
    
    def create_world_step(self, parent):
        """创建世界背景步骤"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=20)
        
        ttk.Label(frame, text="选择世界背景:", font=('微软雅黑', 11)).pack(anchor=tk.W, pady=5)
        
        # 预设选项
        self.world_var = tk.StringVar()
        for i, (name, desc) in enumerate(_WORLD_PRESETS):
            rb = ttk.Radiobutton(frame, text=f"{name} - {desc}", 
                                variable=self.world_var, value=f"{name}——{desc}")
            rb.pack(anchor=tk.W, pady=3)
        
        # 自定义
        custom_frame = ttk.Frame(frame)
        custom_frame.pack(fill=tk.X, pady=10)
        
        ttk.Radiobutton(custom_frame, text="自定义:", 
                       variable=self.world_var, value="custom").pack(side=tk.LEFT)
        
        self.custom_world = ttk.Entry(custom_frame, width=40)
        self.custom_world.pack(side=tk.LEFT, padx=5)
        
        # 随机按钮
        ttk.Button(frame, text="🎲 随机选择", 
                  command=lambda: self.world_var.set(_rng.choice([f"{n}——{d}" for n, d in _WORLD_PRESETS]))
                  ).pack(anchor=tk.W, pady=10)
        
        # 默认值
        if "world_bg" in self.wizard_data:
            self.world_var.set(self.wizard_data["world_bg"])
        else:
            self.world_var.set(f"{_WORLD_PRESETS[0][0]}——{_WORLD_PRESETS[0][1]}")
    
    def create_player_step(self, parent):
        """创建玩家身份步骤"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=20)
        
        # 角色名字
        name_frame = ttk.Frame(frame)
        name_frame.pack(fill=tk.X, pady=10)
        ttk.Label(name_frame, text="角色名字:", width=12).pack(side=tk.LEFT)
        self.player_name = ttk.Entry(name_frame, width=30)
        self.player_name.pack(side=tk.LEFT, padx=5)
        ttk.Button(name_frame, text="🎲", width=3,
                  command=lambda: self.player_name.delete(0, tk.END) or self.player_name.insert(0, "随机")
                  ).pack(side=tk.LEFT)
        
        # 身份
        id_frame = ttk.Frame(frame)
        id_frame.pack(fill=tk.X, pady=10)
        ttk.Label(id_frame, text="身份/背景:", width=12).pack(side=tk.LEFT)
        self.player_identity = ttk.Entry(id_frame, width=30)
        self.player_identity.pack(side=tk.LEFT, padx=5)
        ttk.Button(id_frame, text="🎲", width=3,
                  command=lambda: self.player_identity.delete(0, tk.END) or self.player_identity.insert(0, "随机")
                  ).pack(side=tk.LEFT)
        
        # 特殊能力
        spec_frame = ttk.Frame(frame)
        spec_frame.pack(fill=tk.X, pady=10)
        ttk.Label(spec_frame, text="特殊能力:", width=12).pack(side=tk.LEFT)
        self.player_special = ttk.Entry(spec_frame, width=30)
        self.player_special.pack(side=tk.LEFT, padx=5)
        ttk.Button(spec_frame, text="🎲", width=3,
                  command=lambda: self.player_special.delete(0, tk.END) or self.player_special.insert(0, "随机")
                  ).pack(side=tk.LEFT)
        
        # 填充已有数据
        if "player" in self.wizard_data:
            p = self.wizard_data["player"]
            self.player_name.insert(0, p.get("name", ""))
            self.player_identity.insert(0, p.get("identity", ""))
            self.player_special.insert(0, p.get("special", ""))
        else:
            self.player_name.insert(0, "主角")
            self.player_identity.insert(0, "普通人")
    
    def create_heroines_step(self, parent):
        """创建女主角设定步骤"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=20)

        # 数量选择
        count_frame = ttk.Frame(frame)
        count_frame.pack(fill=tk.X, pady=10)
        ttk.Label(count_frame, text="女主角数量:").pack(side=tk.LEFT)
        self.heroine_count = ttk.Combobox(count_frame, values=[1, 2, 3, 4], width=5, state="readonly")
        self.heroine_count.set(1)
        self.heroine_count.pack(side=tk.LEFT, padx=5)
        def _rand_count():
            self.heroine_count.set(_rng.randint(1, 3))
            self._toggle_heroine_manual()
        ttk.Button(count_frame, text="🎲", width=3, command=_rand_count).pack(side=tk.LEFT)

        # 是否自行设定
        self.heroine_manual = tk.BooleanVar(value=False)
        manual_frame = ttk.Frame(frame)
        manual_frame.pack(fill=tk.X, pady=5)
        ttk.Checkbutton(
            manual_frame,
            text="自行设定每位女主角的性格与外貌（不勾选则由AI随机生成）",
            variable=self.heroine_manual,
            command=self._toggle_heroine_manual,
        ).pack(anchor=tk.W)

        ttk.Label(frame, text=f"性格参考: {_PERSONALITY_HINTS}",
                 font=('微软雅黑', 9), foreground='gray').pack(anchor=tk.W, pady=2)

        # 女主角列表（仅手动设定时显示）
        self.heroines_frame = ttk.Frame(frame)
        self.heroines_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.heroine_entries = []
        # 初始不展开输入框
        self._toggle_heroine_manual()

        # 绑定数量变化
        self.heroine_count.bind("<<ComboboxSelected>>", lambda e: self._toggle_heroine_manual())

    def _toggle_heroine_manual(self):
        """根据复选框状态决定是否显示女主角输入框"""
        for w in self.heroines_frame.winfo_children():
            w.destroy()
        self.heroine_entries = []
        if self.heroine_manual.get():
            self.update_heroine_inputs()
    
    def update_heroine_inputs(self):
        """更新女主角输入框"""
        for widget in self.heroines_frame.winfo_children():
            widget.destroy()
        
        count = int(self.heroine_count.get())
        self.heroine_entries = []
        
        for i in range(count):
            card = ttk.LabelFrame(self.heroines_frame, text=f"女主角 {i+1}", padding=10)
            card.pack(fill=tk.X, pady=5)
            
            # 名字
            name_frame = ttk.Frame(card)
            name_frame.pack(fill=tk.X)
            ttk.Label(name_frame, text="名字:", width=8).pack(side=tk.LEFT)
            name_entry = ttk.Entry(name_frame, width=20)
            name_entry.insert(0, f"女主{i+1}")
            name_entry.pack(side=tk.LEFT, padx=5)
            ttk.Button(name_frame, text="🎲", width=3,
                      command=lambda e=name_entry: e.delete(0, tk.END) or e.insert(0, "随机")
                      ).pack(side=tk.LEFT)
            
            # 性格
            pers_frame = ttk.Frame(card)
            pers_frame.pack(fill=tk.X, pady=5)
            ttk.Label(pers_frame, text="性格:", width=8).pack(side=tk.LEFT)
            pers_entry = ttk.Entry(pers_frame, width=20)
            pers_entry.insert(0, "神秘")
            pers_entry.pack(side=tk.LEFT, padx=5)
            ttk.Button(pers_frame, text="🎲", width=3,
                      command=lambda e=pers_entry: e.delete(0, tk.END) or e.insert(0, "随机")
                      ).pack(side=tk.LEFT)
            
            # 外貌/背景
            desc_frame = ttk.Frame(card)
            desc_frame.pack(fill=tk.X)
            ttk.Label(desc_frame, text="外貌/背景:", width=8).pack(side=tk.LEFT)
            desc_entry = ttk.Entry(desc_frame, width=30)
            desc_entry.pack(side=tk.LEFT, padx=5)
            
            self.heroine_entries.append((name_entry, pers_entry, desc_entry))
    
    def create_tone_step(self, parent):
        """创建游戏基调步骤"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=20)
        
        ttk.Label(frame, text="选择游戏基调（可多选）:", font=('微软雅黑', 11)).pack(anchor=tk.W, pady=5)
        
        self.tone_vars = []
        for tone in _TONE_PRESETS:
            var = tk.BooleanVar()
            cb = ttk.Checkbutton(frame, text=tone, variable=var)
            cb.pack(anchor=tk.W, pady=3)
            self.tone_vars.append((tone, var))
        
        # 自定义
        custom_frame = ttk.Frame(frame)
        custom_frame.pack(fill=tk.X, pady=10)
        ttk.Label(custom_frame, text="自定义基调:").pack(side=tk.LEFT)
        self.custom_tone = ttk.Entry(custom_frame, width=30)
        self.custom_tone.pack(side=tk.LEFT, padx=5)
        
        # 随机
        ttk.Button(frame, text="🎲 随机选择", 
                  command=self.random_tone).pack(anchor=tk.W, pady=10)
        
        # 填充已有数据
        if "tone" in self.wizard_data:
            tones = self.wizard_data["tone"].split("+")
            for tone, var in self.tone_vars:
                var.set(tone in tones)
    
    def random_tone(self):
        """随机选择基调"""
        for tone, var in self.tone_vars:
            var.set(False)
        _rng.choice(self.tone_vars)[1].set(True)
    
    def create_plot_step(self, parent):
        """创建主线剧情步骤"""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True, pady=20)
        
        self.plot_var = tk.StringVar(value="free")
        
        ttk.Radiobutton(frame, text="随GM自由发挥", variable=self.plot_var, value="free").pack(anchor=tk.W, pady=5)
        ttk.Radiobutton(frame, text="有明确主线", variable=self.plot_var, value="custom").pack(anchor=tk.W, pady=5)
        
        self.plot_text = scrolledtext.ScrolledText(frame, width=50, height=8, wrap=tk.WORD)
        self.plot_text.pack(fill=tk.BOTH, expand=True, pady=10)
        
        ttk.Button(frame, text="🎲 随机主线", 
                  command=lambda: self.plot_text.delete(1.0, tk.END) or self.plot_text.insert(1.0, "随机")
                  ).pack(anchor=tk.W)
        
        # 填充已有数据
        if "plot" in self.wizard_data:
            if self.wizard_data["plot"]:
                self.plot_var.set("custom")
                self.plot_text.insert(1.0, self.wizard_data["plot"])
    
    def wizard_next(self):
        """向导下一步"""
        # 保存当前步骤数据
        if self.wizard_step == 0:
            world = self.world_var.get()
            if world == "custom":
                world = self.custom_world.get() or "现代都市"
            self.wizard_data["world_bg"] = world
            
        elif self.wizard_step == 1:
            self.wizard_data["player"] = {
                "name": self.player_name.get() or "主角",
                "identity": self.player_identity.get() or "普通人",
                "special": self.player_special.get(),
            }
            
        elif self.wizard_step == 2:
            count = int(self.heroine_count.get())
            if self.heroine_manual.get() and self.heroine_entries:
                heroines = []
                for name_e, pers_e, desc_e in self.heroine_entries:
                    heroines.append((
                        name_e.get() or "女主",
                        pers_e.get() or "神秘",
                        desc_e.get()
                    ))
            else:
                # AI随机生成：只传入数量，名字/性格留空由AI决定
                heroines = [("", "", "") for _ in range(count)]
            self.wizard_data["heroines"] = heroines
            self.wizard_data["heroines_auto"] = not self.heroine_manual.get()
            
        elif self.wizard_step == 3:
            tones = [t for t, v in self.tone_vars if v.get()]
            custom = self.custom_tone.get()
            if custom:
                tones.append(custom)
            self.wizard_data["tone"] = "+".join(tones) if tones else "浪漫恋爱"
            
        elif self.wizard_step == 4:
            if self.plot_var.get() == "free":
                self.wizard_data["plot"] = ""
            else:
                self.wizard_data["plot"] = self.plot_text.get(1.0, tk.END).strip()
        
        # 下一步或完成
        if self.wizard_step < len(self.wizard_steps) - 1:
            self.show_wizard_step(self.wizard_step + 1)
        else:
            self.finish_wizard()
    
    def wizard_prev(self):
        """向导上一步"""
        if self.wizard_step > 0:
            self.show_wizard_step(self.wizard_step - 1)
    
    def finish_wizard(self):
        """完成向导，生成开局指令"""
        # 构建开局指令
        world_bg = self.wizard_data.get("world_bg", "现代都市")
        player = self.wizard_data.get("player", {})
        heroines = self.wizard_data.get("heroines", [])
        tone = self.wizard_data.get("tone", "浪漫恋爱")
        plot = self.wizard_data.get("plot", "")
        
        lines = [
            "以下是本次游戏的完整世界设定，请严格按照设定展开故事：",
            "",
            f"【世界背景】{world_bg}",
        ]
        
        ident = f"【玩家身份】{player.get('name', '主角')}——{player.get('identity', '普通人')}"
        if player.get('special'):
            ident += f"（特殊能力：{player['special']}）"
        lines.append(ident)
        lines.append("")
        heroines_auto = self.wizard_data.get("heroines_auto", False)
        if heroines_auto:
            lines.append(f"【女主角】共 {len(heroines)} 位，性格、外貌、名字由你（GM）自由创作，风格契合世界背景与游戏基调。")
        else:
            lines.append("【女主角】")
            for h_name, h_personality, h_desc in heroines:
                entry = f"  · {h_name}：{h_personality}性格"
                if h_desc:
                    entry += f"，{h_desc}"
                lines.append(entry)
        
        lines.append("")
        lines.append(f"【游戏基调】{tone}")
        if plot:
            lines.append(f"【主线剧情】{plot}")
        lines.append("")
        lines.append("请直接开始第一个场景，让玩家与第一位女主角自然相遇，无需询问任何设定问题。")
        
        instruction = "\n".join(lines)
        
        # 显示预览对话框
        preview = tk.Toplevel(self.root)
        preview.title("开局指令预览")
        preview.geometry("600x500")
        preview.transient(self.root)
        preview.grab_set()
        
        ttk.Label(preview, text="生成的开局指令:", font=('微软雅黑', 12, 'bold')).pack(pady=10)
        
        text = scrolledtext.ScrolledText(preview, width=60, height=20, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        text.insert(1.0, instruction)
        
        btn_frame = ttk.Frame(preview)
        btn_frame.pack(pady=10)
        
        def start_game():
            preview.destroy()
            self.start_new_game(instruction)
        
        ttk.Button(btn_frame, text="开始游戏", command=start_game).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="返回修改", command=preview.destroy).pack(side=tk.LEFT, padx=5)
    
    def start_new_game(self, instruction: str):
        """开始新游戏"""
        # 输入故事名
        story_name = simpledialog.askstring("故事名称", "请输入故事名称:", 
                                           initialvalue="story1")
        if not story_name:
            story_name = "story1"
        
        self.story_name = story_name
        self.state = empty_state()
        
        # 发送开局指令
        self.set_status("GM构建世界中...")
        self.show_game_interface()
        
        # 在新线程中生成响应
        def generate_response():
            try:
                self.state["round"] += 1
                system = build_system_prompt({}, initial_setting=instruction)
                first_input = build_user_prompt("请按照系统设定直接开始第一个场景。")
                # 开局 history 为空，直接用 generate 即可
                response = generate(system, first_input)
                narrative, updates = parse_response(response)
                apply_updates(self.state.setdefault("world_state", {}), updates)

                # 对齐 main.py send_first_turn：持久化首回合设定，跨会话恢复 system prompt 用
                ws = self.state["world_state"]
                ws["_initial_setting"] = instruction
                if "heroines" not in ws:
                    ws["heroines"] = []

                self.state["history"].append({"role": "user", "content": first_input})
                # add_gm_message 在主线程执行，save=True 会把 narrative 追加到 history
                self.root.after(0, lambda: self.add_gm_message(narrative))
                self.root.after(0, lambda: self.set_status("游戏开始"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("错误", f"游戏启动失败: {e}"))
                self.root.after(0, lambda: self.set_status("启动失败"))
        
        threading.Thread(target=generate_response, daemon=True).start()
    
    # ══════════════════════════════════════════════════════════════════
    # 游戏主界面
    # ══════════════════════════════════════════════════════════════════
    
    def show_game_interface(self):
        """显示游戏主界面"""
        self.clear_frame()
        
        # 创建主框架
        main_frame = ttk.Frame(self.main_container)
        main_frame.pack(fill=tk.BOTH, expand=True)
        self.current_frame = main_frame
        
        # 顶部信息栏
        self.info_frame = ttk.Frame(main_frame)
        self.info_frame.pack(fill=tk.X, pady=5)
        
        self.story_label = ttk.Label(self.info_frame, text=f"故事: {self.story_name}", 
                                    font=('微软雅黑', 12, 'bold'))
        self.story_label.pack(side=tk.LEFT)
        
        self.round_label = ttk.Label(self.info_frame, text=f"第 {self.state['round']} 回合", 
                                    font=('微软雅黑', 11))
        self.round_label.pack(side=tk.LEFT, padx=20)
        
        # 工具按钮
        ttk.Button(self.info_frame, text="💾 存档", command=self.show_save_dialog).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.info_frame, text="📂 读档", command=self.show_load_dialog).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.info_frame, text="📊 状态", command=self.show_status_dialog).pack(side=tk.RIGHT, padx=2)
        ttk.Button(self.info_frame, text="🏠 主菜单", command=self.confirm_return_menu).pack(side=tk.RIGHT, padx=2)
        
        # 对话显示区域
        chat_frame = ttk.LabelFrame(main_frame, text="游戏对话", padding=5)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.chat_text = scrolledtext.ScrolledText(chat_frame, wrap=tk.WORD, 
                                                   font=('微软雅黑', 11), state=tk.DISABLED)
        self.chat_text.pack(fill=tk.BOTH, expand=True)
        self.chat_text.tag_config("user", foreground="#0066cc", font=('微软雅黑', 11, 'bold'))
        self.chat_text.tag_config("gm", foreground="#333333", font=('微软雅黑', 11))
        self.chat_text.tag_config("system", foreground="#666666", font=('微软雅黑', 10, 'italic'))
        
        # 输入区域
        input_frame = ttk.Frame(main_frame)
        input_frame.pack(fill=tk.X, pady=5)
        
        self.input_entry = ttk.Entry(input_frame, font=('微软雅黑', 11))
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.input_entry.bind("<Return>", lambda e: self.send_message())
        
        ttk.Button(input_frame, text="发送", command=self.send_message).pack(side=tk.RIGHT, padx=5)
        
        # 快捷按钮
        quick_frame = ttk.Frame(main_frame)
        quick_frame.pack(fill=tk.X)
        
        quick_commands = [
            ("保存", lambda: self.show_save_dialog()),
            ("查看状态", lambda: self.show_status_dialog()),
            ("/help", lambda: self.show_help()),
        ]
        
        for text, cmd in quick_commands:
            ttk.Button(quick_frame, text=text, command=cmd).pack(side=tk.LEFT, padx=2)
        
        # 加载历史对话
        self.load_chat_history()
        
        # 聚焦输入框
        self.input_entry.focus()
    
    def load_chat_history(self):
        """加载历史对话到显示区域"""
        history = self.state.get("history", [])
        print(f"[DEBUG] load_chat_history: {len(history)} messages")
        
        for msg in history:
            print(f"[DEBUG] loading message: role={msg.get('role')}, content preview={msg.get('content', '')[:50]}...")
            if msg["role"] == "user":
                self.add_user_message(msg["content"], save=False)
            else:
                self.add_gm_message(msg["content"], save=False)
    
    def add_user_message(self, text: str, save: bool = True):
        """添加用户消息"""
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"\n【玩家】\n", "user")
        self.chat_text.insert(tk.END, f"{text}\n", "user")
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)
        
        if save:
            self.state["history"].append({"role": "user", "content": text})
    
    def add_gm_message(self, text: str, save: bool = True):
        """添加GM消息"""
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"\n【GM】\n", "gm")
        self.chat_text.insert(tk.END, f"{text}\n", "gm")
        self.chat_text.insert(tk.END, "─" * 50 + "\n", "system")
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)
        
        if save:
            self.state["history"].append({"role": "assistant", "content": text})
    
    def add_system_message(self, text: str):
        """添加系统消息"""
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, f"[{text}]\n", "system")
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)
    
    def send_message(self):
        """发送消息"""
        text = self.input_entry.get().strip()
        if not text:
            return

        # 纯 "gm" 唤出GM菜单，不发给LLM
        if text.lower() == "gm":
            self.input_entry.delete(0, tk.END)
            self.show_gm_menu()
            return

        self.input_entry.delete(0, tk.END)
        self.add_user_message(text, save=False)  # 显示用原始文本；history 在子线程存 formatted

        self.state["round"] += 1
        self.round_label.config(text=f"第 {self.state['round']} 回合")
        
        self.set_status("GM思考中...")
        self.input_entry.config(state=tk.DISABLED)
        
        def generate_response():
            try:
                ws = self.state.get("world_state", {})
                system = build_system_prompt(ws)
                dyn = build_dynamic_context(ws)
                if text.lower().startswith("gm "):
                    gm_content = text[3:].strip()
                    formatted = (dyn + "\n" if dyn else "") + f"【GM导演指令】{gm_content}"
                else:
                    formatted = (dyn + "\n" if dyn else "") + build_user_prompt(text)

                # 将本轮玩家消息（含动态上下文）存入 history，再带入 LLM
                self.state["history"].append({"role": "user", "content": formatted})
                response = generate_with_history(
                    system,
                    self.state["history"][:-1],  # history 不含本轮，本轮作为 user_input 传入
                    formatted,
                )

                # 解析增量 JSON 块，更新 world_state
                narrative, updates = parse_response(response)
                apply_updates(self.state.setdefault("world_state", {}), updates)

                self.root.after(0, lambda: self.add_gm_message(narrative))
                self.root.after(0, lambda: self.set_status("就绪"))
                self.root.after(0, lambda: self.input_entry.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.input_entry.focus())

                # 检查是否需要摘要
                self.root.after(100, self.check_summarize)

                # 自动存档
                if self.state["round"] % AUTO_SAVE_EVERY == 0:
                    self.root.after(0, self.auto_save)

            except Exception as e:
                self.root.after(0, lambda: self.add_system_message(f"错误: {e}"))
                self.root.after(0, lambda: self.set_status("发生错误"))
                self.root.after(0, lambda: self.input_entry.config(state=tk.NORMAL))

        threading.Thread(target=generate_response, daemon=True).start()
    
    def check_summarize(self):
        """检查是否需要生成摘要"""
        def status_callback(msg):
            self.set_status(msg)
        
        maybe_summarize(self.state, status_callback)
    
    def auto_save(self):
        """自动存档"""
        self.do_save("auto", silent=True)
    
    def do_save(self, label: str = "manual", silent: bool = False):
        """执行存档"""
        try:
            dyn = build_dynamic_context(self.state.get("world_state", {}))
            system = build_system_prompt(self.state.get("world_state", {}))
            user = (dyn + "\n" if dyn else "") + build_save_request_prompt()
            raw = generate(system, user, force_stream=False)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            
            path = save(self.story_name, self.state, raw_json_str=raw, label=label)
            
            try:
                self.state["world_state"] = json.loads(raw)
            except:
                pass
            
            if not silent:
                messagebox.showinfo("存档成功", f"已保存: {path.name if hasattr(path, 'name') else path}")
            else:
                self.set_status(f"自动存档完成 (第{self.state['round']}回合)")
                
        except Exception as e:
            if not silent:
                messagebox.showerror("存档失败", str(e))
            else:
                self.set_status(f"自动存档失败: {e}")
    
    def show_save_dialog(self):
        """显示存档对话框"""
        label = simpledialog.askstring("存档", "请输入存档备注:", initialvalue="manual")
        if label is not None:
            self.do_save(label or "manual")
    
    def show_load_dialog(self):
        """显示读档对话框"""
        saves = list_saves(self.story_name)
        if not saves:
            messagebox.showinfo("读档", "没有可用存档")
            return
        
        dialog = tk.Toplevel(self.root)
        dialog.title("读取存档")
        dialog.geometry("500x400")
        dialog.transient(self.root)
        dialog.grab_set()
        
        ttk.Label(dialog, text="选择要读取的存档:", font=('微软雅黑', 12)).pack(pady=10)
        
        # 存档列表
        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        canvas = tk.Canvas(list_frame, bg='#fafafa', highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        selected = tk.IntVar(value=-1)
        
        for s in saves:
            info = f"第{s['turn']}回合 | {s['label']} | {s['saved_at'][:16]}"
            rb = ttk.Radiobutton(scroll_frame, text=info, variable=selected, value=s['index'])
            rb.pack(anchor=tk.W, pady=3)
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        def do_load():
            idx = selected.get()
            if idx < 0:
                messagebox.showwarning("提示", "请先选择一个存档")
                return
            
            data = load_by_index(self.story_name, idx)
            if not data:
                messagebox.showerror("错误", "读取失败")
                return
            
            restored = restore_state(data)
            self.state.update(restored)
            dialog.destroy()
            
            # 刷新界面
            self.chat_text.config(state=tk.NORMAL)
            self.chat_text.delete(1.0, tk.END)
            self.chat_text.config(state=tk.DISABLED)
            self.load_chat_history()
            self.round_label.config(text=f"第 {self.state['round']} 回合")
            self.set_status(f"已回滚到第 {self.state['round']} 回合")
        
        ttk.Button(dialog, text="读取", command=do_load).pack(pady=10)
    
    def show_status_dialog(self):
        """显示状态对话框"""
        ws = self.state.get("world_state", {})
        
        dialog = tk.Toplevel(self.root)
        dialog.title("游戏状态")
        dialog.geometry("500x600")
        dialog.transient(self.root)
        
        # 创建画布和滚动条
        canvas = tk.Canvas(dialog, bg='#fafafa', highlightthickness=0)
        scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas)
        
        frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=frame, anchor="nw", width=480)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # 基本信息
        ttk.Label(frame, text="游戏状态", font=('微软雅黑', 16, 'bold')).pack(pady=10)
        
        info_text = f"""
当前回合: 第 {self.state['round']} 回合
对话历史: {len(self.state['history'])} 条
摘要长度: {len(self.state['summary'])} 字
"""
        ttk.Label(frame, text=info_text, font=('微软雅黑', 11), justify=tk.LEFT).pack(anchor=tk.W, padx=20)
        
        # 游戏时间
        if ws.get("save_info"):
            si = ws["save_info"]
            time_text = f"\n游戏时间: {si.get('date', '')} {si.get('time_slot', '')} @ {si.get('location', '')}"
            ttk.Label(frame, text=time_text, font=('微软雅黑', 11)).pack(anchor=tk.W, padx=20)
        
        # 玩家信息
        player = ws.get("player", {})
        world = ws.get("world", {})
        
        ttk.Label(frame, text="\n玩家信息", font=('微软雅黑', 13, 'bold')).pack(anchor=tk.W, padx=10, pady=5)
        
        if player:
            player_text = f"身份: {player.get('name', '')} · {player.get('identity', '')}"
        elif world:
            player_text = f"身份: {world.get('player_name', '')} · {world.get('player_identity', '')}"
        else:
            player_text = "身份: 未知"
        
        ttk.Label(frame, text=player_text, font=('微软雅黑', 11)).pack(anchor=tk.W, padx=20)
        
        if world.get("tone"):
            ttk.Label(frame, text=f"基调: {world.get('tone', '')}", font=('微软雅黑', 11)).pack(anchor=tk.W, padx=20)
        
        # 女主角
        heroines = ws.get("heroines", [])
        if heroines:
            ttk.Label(frame, text="\n角色状态", font=('微软雅黑', 13, 'bold')).pack(anchor=tk.W, padx=10, pady=5)
            
            for h in heroines:
                name = h.get("name", "?")
                aff = h.get("affection", 0)
                stage = h.get("stage", "")
                bar = _affection_bar(aff)
                
                hero_text = f"{name}: {bar} {aff} {stage}"
                ttk.Label(frame, text=hero_text, font=('微软雅黑', 11)).pack(anchor=tk.W, padx=20, pady=2)
        
        # 待处理事项
        suspended = ws.get("suspended_issues", [])
        if suspended:
            ttk.Label(frame, text="\n待处理事项", font=('微软雅黑', 13, 'bold')).pack(anchor=tk.W, padx=10, pady=5)
            
            for iss in suspended[:5]:
                char = iss.get("character", "")
                issue = iss.get("issue", "")
                ttk.Label(frame, text=f"· {char}: {issue}", font=('微软雅黑', 10)).pack(anchor=tk.W, padx=20)
        
        # 摘要
        if self.state.get("summary"):
            ttk.Label(frame, text="\n故事摘要", font=('微软雅黑', 13, 'bold')).pack(anchor=tk.W, padx=10, pady=5)
            
            summary_text = scrolledtext.ScrolledText(frame, width=50, height=8, wrap=tk.WORD, font=('微软雅黑', 10))
            summary_text.pack(fill=tk.X, padx=20, pady=5)
            summary_text.insert(1.0, self.state["summary"])
            summary_text.config(state=tk.DISABLED)
        
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=10)
    
    def show_help(self):
        """显示帮助"""
        help_text = """
可用命令:
/save [备注]   - 手动存档
/load          - 列出存档并读取
/status        - 显示当前状态面板
/help          - 显示此帮助

快捷按钮:
💾 存档       - 保存当前进度
📂 读档       - 读取之前的存档
📊 状态       - 查看游戏状态
🏠 主菜单     - 返回主菜单
"""
        messagebox.showinfo("帮助", help_text)
    
    def confirm_return_menu(self):
        """确认返回主菜单"""
        if messagebox.askyesno("确认", "返回主菜单将保留当前进度，确定吗？"):
            self.show_main_menu()

    # ══════════════════════════════════════════════════════════════════
    # GM 菜单
    # ══════════════════════════════════════════════════════════════════

    def show_gm_menu(self):
        """弹出GM菜单窗口。"""
        win = tk.Toplevel(self.root)
        win.title("GM菜单")
        win.geometry("420x300")
        win.transient(self.root)
        win.grab_set()

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # ── Tab：属性修改 ─────────────────────────────────────────
        tab = ttk.Frame(nb)
        nb.add(tab, text="属性修改")
        self._build_attr_tab(tab, win)

    def _build_attr_tab(self, parent: ttk.Frame, win: tk.Toplevel):
        """属性修改 Tab 内容。后续新增字段在此扩展。"""
        ws = self.state.get("world_state", {})
        world = ws.get("world", {})          # 兼容旧存档路径
        player = ws.get("player", world)     # v4 存档里可能在 player 键

        frame = ttk.Frame(parent, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # ── 字段定义：(标签, 当前值读取路径, 写回setter) ──────────
        #   采用列表方便后续扩展更多字段
        fields: list[tuple[str, str, tk.StringVar]] = []

        def _add_field(label: str, current_val: str) -> tk.StringVar:
            var = tk.StringVar(value=current_val)
            row = len(fields)
            ttk.Label(frame, text=label, width=14, anchor=tk.W).grid(
                row=row, column=0, sticky=tk.W, pady=6)
            ttk.Entry(frame, textvariable=var, width=28).grid(
                row=row, column=1, sticky=tk.EW, padx=5)
            fields.append((label, current_val, var))
            return var

        # player_special：从多处兼容读取
        special_val = (
            ws.get("player_special", "")
            or player.get("special", "")
            or world.get("player_special", "")
        )
        var_special = _add_field("特殊能力", special_val)

        # 后续扩展示例（注释保留接口）:
        # var_name = _add_field("玩家姓名", player.get("name", ""))

        frame.columnconfigure(1, weight=1)

        # ── 确认按钮 ──────────────────────────────────────────────
        def _confirm():
            ws = self.state.setdefault("world_state", {})
            # 写回 player_special（写到顶层，兼容 builder.py 读取方式）
            ws["player_special"] = var_special.get().strip()
            # 同步到 player / world 子键（若存在）
            if "player" in ws:
                ws["player"]["special"] = ws["player_special"]
            if "world" in ws:
                ws["world"]["player_special"] = ws["player_special"]
            win.destroy()

        ttk.Button(parent, text="确认", command=_confirm).pack(pady=10)


def main():
    root = tk.Tk()
    app = GameApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
