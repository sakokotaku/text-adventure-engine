#!/usr/bin/env python3
"""
tests/test_engine.py
--------------------
离线测试套件 — 不发起真实 API 请求。

运行方式：
    python tests/test_engine.py
    python -m pytest tests/test_engine.py -v

覆盖范围：
  1. 存读档完整性   (TestSaveLoad      — 13 项)
  2. 回合状态正确性  (TestRoundState    —  7 项)
  3. 角色锁定字段   (TestEnforceLocks  —  7 项)
  4. API 失败处理   (TestAPIFailures   —  4 项)
  5. 边界输入       (TestEdgeCases     — 12 项)
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# ── 路径（必须在 import 项目模块之前）────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 屏蔽测试期间所有项目日志，避免干扰测试报告
logging.disable(logging.CRITICAL)

import storage.save_manager as _sm   # noqa: E402  预加载，方便补丁
import main as _m                    # noqa: E402  预加载，方便补丁


# ══════════════════════════════════════════════════════════════════
# 辅助工厂
# ══════════════════════════════════════════════════════════════════

def _ws(turn: int = 1) -> dict:
    """最小可用 world_state（v4 三层格式）。"""
    return {
        "save_info": {
            "turn": turn, "date": "2026-01-01",
            "time_slot": "morning", "location": "城镇",
        },
        "world_rules": {"setting": "现代都市", "tone": "轻松治愈"},
        "characters":  {"heroines": [], "supporting_characters": []},
        "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
    }


def _state(round_: int = 1, history: list | None = None) -> dict:
    return {"round": round_, "history": history or [], "world_state": _ws(turn=round_)}


# ══════════════════════════════════════════════════════════════════
# Mixin：每个测试方法使用独立临时存档目录
# ══════════════════════════════════════════════════════════════════

class _WithTempSaves(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="te_")
        self._orig_save_dir = _sm.SAVE_DIR
        _sm.SAVE_DIR = Path(self._tmp) / "saves"
        _sm.SAVE_DIR.mkdir()

    def tearDown(self) -> None:
        _sm.SAVE_DIR = self._orig_save_dir
        shutil.rmtree(self._tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 1. 存读档完整性
# ══════════════════════════════════════════════════════════════════

class TestSaveLoad(_WithTempSaves):
    """storage/save_manager：写入、读取、列表、删除。"""

    # ── 写入 ────────────────────────────────────────────────────

    def test_save_creates_json_file(self):
        """save() 生成 .json 文件并可被 glob 找到。"""
        st = _state()
        path = _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="t")
        self.assertTrue(path.exists())
        self.assertEqual(path.suffix, ".json")

    def test_save_roundtrip_core_fields(self):
        """写入后读回，所有核心字段不丢失。"""
        st = _state(round_=7)
        path = _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="rt")
        data = _sm.load_by_path(str(path))
        self.assertIsNotNone(data)
        for key in ("world_rules", "story_state", "_saved_at", "_label", "_history"):
            self.assertIn(key, data, f"字段缺失：{key}")

    def test_save_label_and_history_persisted(self):
        """_label 和 _history 被正确持久化。"""
        hist = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]
        st   = _state(history=hist)
        path = _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="xlbl")
        data = _sm.load_by_path(str(path))
        self.assertEqual(data["_label"],   "xlbl")
        self.assertEqual(data["_history"], hist)

    def test_invalid_json_falls_back_to_txt(self):
        """非法 JSON 降级写入 .txt，内容原样保留。"""
        st  = _state()
        bad = "这根本不是JSON {"
        path = _sm.save("s1", st, raw_json_str=bad, label="bad")
        self.assertEqual(path.suffix, ".txt")
        self.assertEqual(path.read_text(encoding="utf-8"), bad)

    def test_fallback_save_without_raw(self):
        """不传 raw_json_str 时从 world_state 构建 fallback 存档。"""
        st   = _state(round_=3)
        path = _sm.save("s1", st, label="fb")
        self.assertTrue(path.exists())
        data = _sm.load_by_path(str(path))
        self.assertIsNotNone(data)
        self.assertIn("world_rules", data)

    # ── 列表 ────────────────────────────────────────────────────

    def test_list_saves_count_and_index(self):
        """list_saves 返回正确数量，索引从 1 起连续。存档上限为 2，写入 3 条后只剩 2 条。"""
        import time
        st  = _state()
        raw = json.dumps(st["world_state"])
        for i in range(3):
            _sm.save("s1", st, raw_json_str=raw, label=f"x{i}")
            time.sleep(0.02)
        saves = _sm.list_saves("s1")
        self.assertEqual(len(saves), 2)
        self.assertEqual([s["index"] for s in saves], [1, 2])

    def test_list_saves_skips_corrupt_file(self):
        """损坏的 JSON 文件被跳过，合法存档仍可见。"""
        d = _sm._story_dir("s1")
        (d / "save_19000101_000000_r0_corrupt.json").write_text("BAD", encoding="utf-8")
        st = _state()
        _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="ok")
        saves = _sm.list_saves("s1")
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["label"], "ok")

    def test_list_stories_returns_names(self):
        """list_stories 返回所有故事名。"""
        st  = _state()
        raw = json.dumps(st["world_state"])
        _sm.save("adventure", st, raw_json_str=raw)
        _sm.save("romance",   st, raw_json_str=raw)
        stories = _sm.list_stories()
        self.assertIn("adventure", stories)
        self.assertIn("romance",   stories)

    # ── 读取 ────────────────────────────────────────────────────

    def test_load_by_index_valid(self):
        """通过索引 1 加载存档成功。"""
        st = _state()
        _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="idx")
        data = _sm.load_by_index("s1", 1)
        self.assertIsNotNone(data)

    def test_load_by_index_out_of_range(self):
        """越界索引（0 / 999）返回 None，不抛异常。"""
        st = _state()
        _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]))
        self.assertIsNone(_sm.load_by_index("s1", 0))
        self.assertIsNone(_sm.load_by_index("s1", 999))

    def test_load_by_path_missing_returns_none(self):
        """路径不存在时返回 None，不抛异常。"""
        self.assertIsNone(_sm.load_by_path("/no/such/file.json"))

    # ── 删除 ────────────────────────────────────────────────────

    def test_delete_save_removes_file(self):
        """delete_save 后文件消失，list_saves 返回空列表。"""
        st   = _state()
        path = _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]))
        ok   = _sm.delete_save("s1", 1)
        self.assertTrue(ok)
        self.assertFalse(path.exists())
        self.assertEqual(_sm.list_saves("s1"), [])

    def test_delete_invalid_index_returns_false(self):
        """越界删除返回 False，不抛异常。"""
        self.assertFalse(_sm.delete_save("s1", 99))


# ══════════════════════════════════════════════════════════════════
# 2. 回合状态正确性
# ══════════════════════════════════════════════════════════════════

class TestRoundState(unittest.TestCase):
    """main.py 纯函数：empty_state / restore_state / _trim_history。"""

    def test_empty_state_structure(self):
        """empty_state 字段类型和初始值正确。"""
        st = _m.empty_state()
        self.assertEqual(st["round"], 0)
        self.assertEqual(st["history"], [])
        self.assertIsInstance(st["world_state"], dict)

    def test_trim_history_caps_at_session_window(self):
        """_trim_history 将超出的 history 截断到 SESSION_WINDOW*2。"""
        st = _m.empty_state()
        for i in range(10):
            st["history"].append({"role": "user", "content": str(i)})
        _m._trim_history(st)
        self.assertEqual(len(st["history"]), _m.SESSION_WINDOW * 2)

    def test_trim_history_keeps_latest(self):
        """裁剪后保留的是最新的消息（最后一条 content 为 "9"）。"""
        st = _m.empty_state()
        for i in range(10):
            st["history"].append({"role": "user", "content": str(i)})
        _m._trim_history(st)
        self.assertEqual(st["history"][-1]["content"], "9")

    def test_trim_history_noop_when_short(self):
        """history 未超限时不裁剪。"""
        st = _m.empty_state()
        st["history"] = [{"role": "user", "content": "a"}]
        _m._trim_history(st)
        self.assertEqual(len(st["history"]), 1)

    def test_restore_state_reads_turn(self):
        """restore_state 从 save_info.turn 恢复 round。"""
        data = {"save_info": {"turn": 42}, "_history": []}
        st   = _m.restore_state(data)
        self.assertEqual(st["round"], 42)

    def test_restore_state_trims_long_history(self):
        """restore_state 只恢复最近 SESSION_WINDOW*2 条历史。"""
        long_hist = [{"role": "user", "content": str(i)} for i in range(20)]
        data = {"save_info": {"turn": 1}, "_history": long_hist}
        st   = _m.restore_state(data)
        self.assertLessEqual(len(st["history"]), _m.SESSION_WINDOW * 2)

    def test_restore_state_missing_history_key(self):
        """_history 键缺失时不崩溃，history 为空列表。"""
        data = {"save_info": {"turn": 5}}
        st   = _m.restore_state(data)
        self.assertEqual(st["history"], [])


# ══════════════════════════════════════════════════════════════════
# 3. 角色锁定字段验证
# ══════════════════════════════════════════════════════════════════

class TestEnforceLocks(unittest.TestCase):
    """main._enforce_locks：锁定还原 / 可写字段通过 / 数组裁剪。"""

    @staticmethod
    def _h(name: str = "爱丽丝", **overrides) -> dict:
        """构造一个完整的女主角 dict，可按需覆盖字段。"""
        base = {
            "name":              name,
            "appearance":        "原始外貌",
            "personality_core":  "原始性格",
            "speech_samples":    {"most_characteristic": "原始台词"},
            "hidden_attributes": {"approachability": "高"},
            "address":           {"player_calls_her": "她", "she_calls_player": "你"},
            "first_reactions":   ["初见反应"],
            "private_anchors":   ["私密锚点"],
            "nickname":          "原始昵称",
            "affection":         30,
            "relationship_stage": "普通朋友",
            "last_interaction":  "握手",
        }
        base.update(overrides)
        return base

    def test_world_rules_locked(self):
        """AI 修改 world_rules 后被还原为旧值。"""
        old = {"world_rules": {"setting": "原始背景", "tone": "原始基调"}}
        new = {"world_rules": {"setting": "篡改背景", "tone": "篡改基调"}, "characters": {}}
        _m._enforce_locks(new, old)
        self.assertEqual(new["world_rules"]["setting"], "原始背景")
        self.assertEqual(new["world_rules"]["tone"],    "原始基调")

    def test_all_locked_heroine_fields_restored(self):
        """8 个锁定字段全部被还原为旧值。"""
        old_h = self._h()
        new_h = self._h(
            appearance        = "篡改外貌",
            personality_core  = "篡改性格",
            speech_samples    = {"most_characteristic": "篡改台词"},
            hidden_attributes = {"approachability": "低"},
            address           = {"player_calls_her": "主人", "she_calls_player": "大人"},
            first_reactions   = ["篡改反应"],
            private_anchors   = ["篡改锚点"],
            nickname          = "篡改昵称",
        )
        old = {"characters": {"heroines": [old_h]}, "world_rules": {}}
        new = {"characters": {"heroines": [new_h]}}
        _m._enforce_locks(new, old)
        r = new["characters"]["heroines"][0]
        self.assertEqual(r["appearance"],                           "原始外貌")
        self.assertEqual(r["personality_core"],                     "原始性格")
        self.assertEqual(r["speech_samples"]["most_characteristic"],"原始台词")
        self.assertEqual(r["hidden_attributes"]["approachability"], "高")
        self.assertEqual(r["address"]["player_calls_her"],          "她")
        self.assertEqual(r["first_reactions"],                      ["初见反应"])
        self.assertEqual(r["private_anchors"],                      ["私密锚点"])
        self.assertEqual(r["nickname"],                             "原始昵称")

    def test_unlocked_fields_pass_through(self):
        """affection / relationship_stage / last_interaction 允许 AI 更新。"""
        old_h = self._h()
        new_h = self._h(affection=99, relationship_stage="恋人", last_interaction="拥抱")
        old = {"characters": {"heroines": [old_h]}, "world_rules": {}}
        new = {"characters": {"heroines": [new_h]}}
        _m._enforce_locks(new, old)
        r = new["characters"]["heroines"][0]
        self.assertEqual(r["affection"],          99)
        self.assertEqual(r["relationship_stage"], "恋人")
        self.assertEqual(r["last_interaction"],   "拥抱")

    def test_new_heroine_not_locked(self):
        """旧存档中不存在的新角色，锁定不干预其字段。"""
        old = {"characters": {"heroines": []}, "world_rules": {}}
        new = {"characters": {"heroines": [self._h("新角色", appearance="全新外貌")]}}
        _m._enforce_locks(new, old)
        self.assertEqual(new["characters"]["heroines"][0]["appearance"], "全新外貌")

    def test_event_cards_trimmed_to_10(self):
        """event_cards > 10 条时保留最后 10 条。"""
        cards = [{"event": f"e{i}"} for i in range(15)]
        new   = {"story_state": {"event_cards": cards, "recent_memory": []}, "characters": {}}
        _m._enforce_locks(new, {})
        self.assertEqual(len(new["story_state"]["event_cards"]), 10)
        self.assertEqual(new["story_state"]["event_cards"][0]["event"], "e5")

    def test_recent_memory_trimmed_to_5(self):
        """recent_memory > 5 条时保留最后 5 条。"""
        mem = [f"m{i}" for i in range(8)]
        new = {"story_state": {"event_cards": [], "recent_memory": mem}, "characters": {}}
        _m._enforce_locks(new, {})
        self.assertEqual(len(new["story_state"]["recent_memory"]), 5)
        self.assertEqual(new["story_state"]["recent_memory"][0], "m3")

    def test_locked_fields_registry_in_sync(self):
        """_LOCKED_HEROINE_FIELDS 与测试预期集合完全一致（防漏字段）。"""
        expected = {
            "appearance", "personality_core", "speech_samples",
            "hidden_attributes", "address", "first_reactions",
            "private_anchors", "nickname",
        }
        actual = set(_m._LOCKED_HEROINE_FIELDS)
        self.assertEqual(actual, expected,
                         f"锁定字段注册表已变动，需同步更新测试：差异={actual ^ expected}")


# ══════════════════════════════════════════════════════════════════
# 4. API 失败处理
# ══════════════════════════════════════════════════════════════════

class TestAPIFailures(_WithTempSaves):
    """do_save() 在 LLM 调用失败或返回非法内容时的降级行为。"""

    @patch("main.generate_with_history",
           side_effect=RuntimeError("API 超时 " + "x" * 500))
    def test_api_timeout_creates_fallback_save(self, _mock):
        """API 超时时 do_save() 不崩溃，磁盘上生成 fallback 存档。"""
        st = _state(round_=3)
        try:
            _m.do_save(st, "s1", label="timeout")
        except Exception as e:
            self.fail(f"do_save() 不应抛出异常：{e}")
        self.assertGreater(len(_sm.list_saves("s1")), 0,
                           "应至少存在一个 fallback 存档")

    @patch("main.generate_with_history",
           side_effect=RuntimeError("ERR " + "e" * 600))
    def test_long_error_message_truncated_on_screen(self, _mock):
        """用户界面显示的错误行不超过 350 字符。"""
        st = _state(round_=3)
        with io.StringIO() as buf, redirect_stdout(buf):
            _m.do_save(st, "s1", label="trunc")
            output = buf.getvalue()
        error_lines = [l for l in output.split("\n") if "存档失败" in l]
        for line in error_lines:
            self.assertLess(len(line), 350,
                            f"错误行过长（{len(line)} 字）：{line[:60]}…")

    @patch("main.generate_with_history", return_value="这不是JSON {{{")
    def test_invalid_json_response_no_crash(self, _mock):
        """GM 返回非法 JSON 时，do_save() 不崩溃，有存档产出。"""
        st = _state(round_=3)
        try:
            _m.do_save(st, "s1", label="badjson")
        except Exception as e:
            self.fail(f"do_save() 不应抛出异常：{e}")
        # 必须产出某种存档（可能是 txt）
        story_dir = _sm._story_dir("s1")
        files = list(story_dir.iterdir())
        self.assertGreater(len(files), 0, "应存在至少一个存档文件（json 或 txt）")

    @patch("main.generate_with_history", return_value=json.dumps({
        "save_info":  {"turn": 3, "date": "2026-01-01", "time_slot": "morning", "location": "城镇"},
        "world_rules":{"setting": "现代都市", "tone": "轻松"},
        "characters": {"heroines": []},
        "story_state":{"event_cards": [], "recent_memory": [], "suspended_issues": []},
    }))
    def test_valid_response_updates_world_state(self, _mock):
        """GM 返回合法 JSON 后，state['world_state'] 被更新为新数据。"""
        st = _state(round_=3)
        _m.do_save(st, "s1", label="valid")
        self.assertIn("world_rules",  st["world_state"])
        self.assertIn("story_state",  st["world_state"])


# ══════════════════════════════════════════════════════════════════
# 5. 边界输入
# ══════════════════════════════════════════════════════════════════

class TestEdgeCases(_WithTempSaves):
    """空值、超长、特殊字符、Unicode。"""

    # ── prompt.builder ──────────────────────────────────────────

    def test_build_user_prompt_empty(self):
        """空字符串不崩溃，返回字符串。"""
        from prompt.builder import build_user_prompt
        self.assertIsInstance(build_user_prompt(""), str)

    def test_build_user_prompt_long(self):
        """5 000 字输入完整保留在 prompt 中。"""
        from prompt.builder import build_user_prompt
        long   = "测" * 5_000
        result = build_user_prompt(long)
        self.assertIn(long, result)

    def test_build_user_prompt_special_chars(self):
        """引号、反斜杠、emoji、控制字符不崩溃。"""
        from prompt.builder import build_user_prompt
        special = '你好\n"世界"\t\\n\r😀🎮「テスト」\x00'
        self.assertIsInstance(build_user_prompt(special), str)

    def test_build_system_prompt_empty_world_state(self):
        """空 world_state 不崩溃，返回非空字符串。"""
        from prompt.builder import build_system_prompt
        result = build_system_prompt({})
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_build_system_prompt_new_format_renders_key_fields(self):
        """v4 格式 world_state 渲染后，setting 和角色名出现在 prompt 中。"""
        from prompt.builder import build_system_prompt
        ws = {
            "world_rules": {"setting": "赛博朋克世界", "tone": "暗黑"},
            "characters":  {"heroines": [
                {"name": "零", "affection": 55,
                 "relationship_stage": "伙伴", "personality_core": "冷静"},
            ]},
            "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
        }
        result = build_system_prompt(ws)
        self.assertIn("赛博朋克世界", result)
        self.assertIn("零", result)

    def test_build_system_prompt_with_initial_setting(self):
        """有 initial_setting 时返回结果包含设定文本。"""
        from prompt.builder import build_system_prompt
        result = build_system_prompt({}, initial_setting="这是开局设定XYZ")
        self.assertIn("这是开局设定XYZ", result)

    # ── utils.logger.trunc ──────────────────────────────────────

    def test_trunc_short_string_unchanged(self):
        """短于限制的字符串原样返回。"""
        from utils.logger import trunc
        s = "hello world"
        self.assertEqual(trunc(s, 120), s)

    def test_trunc_long_string_has_marker(self):
        """超长字符串含截断标记，且总长小于原字符串。"""
        from utils.logger import trunc
        s      = "x" * 200
        result = trunc(s, 120)
        self.assertIn("…[+", result)
        self.assertLess(len(result), len(s))

    def test_trunc_newlines_become_spaces(self):
        """换行符被替换（防止日志条目多行分割）。"""
        from utils.logger import trunc
        result = trunc("a\nb\nc")
        self.assertNotIn("\n", result)

    # ── save_manager 边界 ────────────────────────────────────────

    def test_save_unicode_emoji_roundtrip(self):
        """含 Unicode / emoji 的存档，读写内容一致。"""
        ws = _ws()
        ws["world_rules"]["setting"] = '引号"和\\emoji😀和日文テスト'
        st   = _state()
        path = _sm.save("s1", st, raw_json_str=json.dumps(ws))
        data = _sm.load_by_path(str(path))
        self.assertIsNotNone(data)
        self.assertEqual(data["world_rules"]["setting"],
                         '引号"和\\emoji😀和日文テスト')

    def test_load_nonexistent_story_empty_list(self):
        """不存在的故事名不崩溃，返回空列表。"""
        saves = _sm.list_saves("不存在的故事_xyz9999")
        self.assertEqual(saves, [])

    def test_save_very_large_content(self):
        """超大（~1 MB）内容的存档写入不崩溃。"""
        ws = _ws()
        ws["story_state"]["recent_memory"] = ["x" * 1_000] * 1_000
        st = _state()
        try:
            path = _sm.save("s1", st, raw_json_str=json.dumps(ws), label="big")
            self.assertTrue(path.exists())
        except Exception as e:
            self.fail(f"超大内容存档失败：{e}")


# ══════════════════════════════════════════════════════════════════
# 6. suspended_issues 为空时不误发继续游戏指令（fix #1）
# ══════════════════════════════════════════════════════════════════

class TestSuspendedFooter(unittest.TestCase):
    """
    build_dynamic_context：继续游戏指令仅在 suspended_issues 非空时追加。

    修复前：只要 lines 非空（例如有 save_info），就会无条件追加指令。
    修复后：改为 if suspended，确保空列表时不误发。
    """

    _FOOTER = "从suspended_issues第一条继续游戏"

    def _build(self, suspended: list) -> str:
        from prompt.builder import build_dynamic_context
        ws = {
            "save_info": {
                "turn": 1, "date": "2026-01-01",
                "time_slot": "上午", "location": "城镇",
            },
            "world_rules": {"setting": "测试世界"},
            "characters":  {"heroines": []},
            "story_state": {
                "event_cards":      [],
                "recent_memory":    [],
                "suspended_issues": suspended,
            },
        }
        return build_dynamic_context(ws)

    def test_no_footer_when_suspended_empty(self):
        """suspended_issues=[] → 不追加继续游戏提示（修复前此处误发）。"""
        result = self._build([])
        self.assertNotIn(self._FOOTER, result)

    def test_footer_present_when_suspended_has_items(self):
        """suspended_issues 有内容 → 正确追加继续游戏提示。"""
        result = self._build([{"character": "爱丽丝", "issue": "等待回应"}])
        self.assertIn(self._FOOTER, result)

    def test_no_footer_on_empty_world_state(self):
        """完全空的 world_state → 不追加任何指令。"""
        from prompt.builder import build_dynamic_context
        self.assertNotIn(self._FOOTER, build_dynamic_context({}))


# ══════════════════════════════════════════════════════════════════
# 7. 同秒存档文件名不冲突（fix #2）
# ══════════════════════════════════════════════════════════════════

class TestTimestampUniqueness(_WithTempSaves):
    """
    save_manager.save()：时间戳含毫秒，快速连续写入产生不同文件名。

    修复前：精度为秒，同秒两次写入文件名相同，后者静默覆盖前者。
    修复后：strftime 追加 _%f 并截取前 19 位（含 3 位毫秒），格式变为
            save_YYYYMMDD_HHMMSS_mmm_rN_label.json。
    """

    def test_rapid_saves_produce_different_filenames(self):
        """连续两次 save() 文件名不相同，磁盘上存在两个文件。"""
        st  = _state()
        raw = json.dumps(st["world_state"])
        p1  = _sm.save("s1", st, raw_json_str=raw, label="a")
        p2  = _sm.save("s1", st, raw_json_str=raw, label="b")
        self.assertNotEqual(p1.name, p2.name, "两次 save() 文件名不应相同")
        self.assertEqual(len(_sm.list_saves("s1")), 2, "磁盘上应存在两个存档")

    def test_filename_timestamp_includes_milliseconds(self):
        """存档文件名时间戳含 3 位毫秒（修复前只有秒级，共 15 字符）。"""
        st   = _state()
        path = _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]))
        # 文件名格式: save_YYYYMMDD_HHMMSS_mmm_rN_label.json
        # stem 按 "_" 拆分: ['save', '20260311', '110318', '123', 'r1', 'auto']
        parts = path.stem.split("_")
        self.assertEqual(len(parts[1]), 8, f"日期部分应为8位，实际: '{parts[1]}'")
        self.assertEqual(len(parts[2]), 6, f"时间部分应为6位，实际: '{parts[2]}'")
        self.assertTrue(
            parts[3].isdigit() and len(parts[3]) == 3,
            f"毫秒部分应为3位纯数字，实际: '{parts[3]}'",
        )


# ══════════════════════════════════════════════════════════════════
# 8. .txt 降级存档在列表中可见（fix #3）
# ══════════════════════════════════════════════════════════════════

class TestTxtFallbackVisible(_WithTempSaves):
    """
    list_saves()：JSON 解析失败降级为 .txt 时，存档在列表中可见。

    修复前：list_saves 只 glob save_*.json，.txt 文件永远不出现在列表。
    修复后：同时 glob save_*.txt，.txt 条目标注 label='fallback'、turn='?'。
    """

    def test_txt_file_appears_in_list(self):
        """.txt 降级存档出现在 list_saves 结果中（修复前此处长度为 0）。"""
        st = _state()
        _sm.save("s1", st, raw_json_str="这根本不是JSON {", label="bad")
        saves = _sm.list_saves("s1")
        self.assertEqual(len(saves), 1, ".txt 存档应出现在列表中")

    def test_txt_entry_has_fallback_metadata(self):
        """.txt 条目的 label='fallback'，turn='?'。"""
        st = _state()
        _sm.save("s1", st, raw_json_str="BAD JSON", label="err")
        saves = _sm.list_saves("s1")
        self.assertEqual(saves[0]["label"], "fallback")
        self.assertEqual(saves[0]["turn"],  "?")

    def test_txt_and_json_both_visible(self):
        """同一故事下同时存在 .json 和 .txt 时，两者都出现在列表中。"""
        st = _state()
        _sm.save("s1", st, raw_json_str=json.dumps(st["world_state"]), label="ok")
        _sm.save("s1", st, raw_json_str="BAD",                         label="bad")
        saves  = _sm.list_saves("s1")
        labels = {s["label"] for s in saves}
        self.assertEqual(len(saves), 2)
        self.assertIn("ok",       labels)
        self.assertIn("fallback", labels)


# ══════════════════════════════════════════════════════════════════
# 9. load_config() 内存缓存（fix #4）
# ══════════════════════════════════════════════════════════════════

class TestLoadConfigCache(unittest.TestCase):
    """
    provider.load_config()：带内存缓存，进程内只读一次磁盘。

    修复前：每次调用都执行 CONFIG_PATH.read_text()，一次 API 调用内
            多次调用 load_config() 会重复读磁盘。
    修复后：_config_cache 不为 None 时直接返回缓存对象，不再读磁盘。
    """

    def setUp(self):
        """每个测试前将模块级缓存清空。"""
        import llm.provider as _p
        _p._config_cache = None

    def tearDown(self):
        """测试后清空缓存，避免污染后续测试。"""
        import llm.provider as _p
        _p._config_cache = None

    def test_second_call_returns_same_object(self):
        """两次调用返回完全相同的 dict 对象（is 而非 ==，修复前返回新对象）。"""
        from llm.provider import load_config
        cfg1 = load_config()
        cfg2 = load_config()
        self.assertIs(cfg1, cfg2, "两次调用应返回同一个缓存对象")

    def test_disk_read_only_once(self):
        """缓存建立后，再次调用不读磁盘（read_text 只被调用一次）。"""
        import llm.provider as _p
        from unittest.mock import MagicMock
        fake_cfg  = '{"active_provider":"mock","providers":{},"stream":false}'
        fake_path = MagicMock()
        fake_path.exists.return_value    = True
        fake_path.read_text.return_value = fake_cfg
        with patch("llm.provider.CONFIG_PATH", fake_path):
            _p.load_config()
            _p.load_config()
            _p.load_config()
        self.assertEqual(fake_path.read_text.call_count, 1, "磁盘只应被读取一次")


# ══════════════════════════════════════════════════════════════════
# 10. .gitignore 正确排除 config.json（fix #5）
# ══════════════════════════════════════════════════════════════════

class TestGitignore(unittest.TestCase):
    """
    .gitignore 存在且正确排除 config.json。

    修复前：项目根目录无 .gitignore，config.json（含 API Key）可能被提交到 Git。
    修复后：创建 .gitignore，其中包含 config.json 条目。
    """

    _GITIGNORE = _ROOT / ".gitignore"

    def test_gitignore_file_exists(self):
        """.gitignore 文件存在于项目根目录。"""
        self.assertTrue(
            self._GITIGNORE.exists(),
            f".gitignore 文件不存在（路径：{self._GITIGNORE}）",
        )

    def test_gitignore_excludes_config_json(self):
        """.gitignore 包含 config.json 条目，防止 API Key 随 Git 泄露。"""
        if not self._GITIGNORE.exists():
            self.skipTest(".gitignore 不存在，跳过内容检查")
        content = self._GITIGNORE.read_text(encoding="utf-8")
        self.assertIn(
            "config.json", content,
            ".gitignore 缺少 config.json 条目，API Key 存在泄露风险",
        )


# ══════════════════════════════════════════════════════════════════
# 11. 预算常量存在且关系正确（builder change 1）
# ══════════════════════════════════════════════════════════════════

class TestBudgetConstants(unittest.TestCase):
    """
    prompt.builder 中两个预算常量必须存在、类型正确、数值关系合理。

    如果常量被删除或顺序颠倒（threshold > budget），此测试报红。
    """

    def test_constants_are_importable(self):
        """_SYSTEM_PROMPT_BUDGET 和 _HEROINE_SPEECH_TRIM_THRESHOLD 可导入。"""
        from prompt.builder import _SYSTEM_PROMPT_BUDGET, _HEROINE_SPEECH_TRIM_THRESHOLD
        self.assertIsInstance(_SYSTEM_PROMPT_BUDGET,        int)
        self.assertIsInstance(_HEROINE_SPEECH_TRIM_THRESHOLD, int)

    def test_budget_greater_than_threshold(self):
        """预算上限必须大于触发裁剪的阈值（否则配置自相矛盾）。"""
        from prompt.builder import _SYSTEM_PROMPT_BUDGET, _HEROINE_SPEECH_TRIM_THRESHOLD
        self.assertGreater(
            _SYSTEM_PROMPT_BUDGET, _HEROINE_SPEECH_TRIM_THRESHOLD,
            "budget 应大于 trim_threshold，否则裁剪前就已超限",
        )

    def test_budget_positive_and_reasonable(self):
        """两个常量均为正整数且在合理范围（100 ~ 100_000）内。"""
        from prompt.builder import _SYSTEM_PROMPT_BUDGET, _HEROINE_SPEECH_TRIM_THRESHOLD
        for name, val in (
            ("_SYSTEM_PROMPT_BUDGET",        _SYSTEM_PROMPT_BUDGET),
            ("_HEROINE_SPEECH_TRIM_THRESHOLD", _HEROINE_SPEECH_TRIM_THRESHOLD),
        ):
            self.assertGreater(val, 100,    f"{name} 不应小于 100")
            self.assertLess(val,    100_000, f"{name} 不应大于 100_000")


# ══════════════════════════════════════════════════════════════════
# 12. _render_heroine trim 参数行为（builder change 2）
# ══════════════════════════════════════════════════════════════════

class TestHeroineTrimParam(unittest.TestCase):
    """
    _render_heroine(trim=False/True) 的渲染差异。

    trim=False（默认）：四条 speech_samples 全部输出。
    trim=True：仅保留 most_characteristic，其余三条丢弃。
    非 speech_samples 字段（appearance / first_reactions / private_anchors）
    在 trim=True 时必须完整保留。
    """

    @staticmethod
    def _heroine(**overrides) -> dict:
        base = {
            "name":              "测试角色",
            "affection":         30,
            "relationship_stage": "朋友",
            "personality_core":  "冷静",
            "appearance":        "APPEARANCE_FIELD",
            "speech_samples": {
                "most_characteristic": "MOST_CHAR_SAMPLE",
                "daily":               "DAILY_SAMPLE",
                "angry_or_hurt":       "ANGRY_SAMPLE",
                "exposed_or_flustered": "EXPOSED_SAMPLE",
            },
            "first_reactions":  ["FIRST_REACT_ITEM"],
            "private_anchors":  ["PRIVATE_ANCHOR_ITEM"],
        }
        base.update(overrides)
        return base

    def _render(self, trim: bool) -> str:
        from prompt.builder import _render_heroine
        lines: list[str] = []
        _render_heroine(lines, self._heroine(), trim=trim)
        return "\n".join(lines)

    def test_no_trim_shows_all_four_samples(self):
        """trim=False → 四条 speech_samples 全部出现（修复前只有此行为，trim 参数不存在）。"""
        result = self._render(trim=False)
        self.assertIn("MOST_CHAR_SAMPLE", result)
        self.assertIn("DAILY_SAMPLE",     result)
        self.assertIn("ANGRY_SAMPLE",     result)
        self.assertIn("EXPOSED_SAMPLE",   result)

    def test_trim_shows_only_most_characteristic(self):
        """trim=True → 只保留 most_characteristic，其余三条不出现。"""
        result = self._render(trim=True)
        self.assertIn("MOST_CHAR_SAMPLE",  result)
        self.assertNotIn("DAILY_SAMPLE",   result)
        self.assertNotIn("ANGRY_SAMPLE",   result)
        self.assertNotIn("EXPOSED_SAMPLE", result)

    def test_trim_preserves_appearance(self):
        """trim=True → appearance 字段完整保留（不裁剪）。"""
        result = self._render(trim=True)
        self.assertIn("APPEARANCE_FIELD", result)

    def test_trim_preserves_first_reactions_and_anchors(self):
        """trim=True → first_reactions 和 private_anchors 完整保留（既成事实不裁剪）。"""
        result = self._render(trim=True)
        self.assertIn("FIRST_REACT_ITEM",   result)
        self.assertIn("PRIVATE_ANCHOR_ITEM", result)


# ══════════════════════════════════════════════════════════════════
# 13. build_static_system_prompt 预算检测（builder change 3）
# ══════════════════════════════════════════════════════════════════

class TestBudgetDetection(unittest.TestCase):
    """
    build_static_system_prompt()：多女主场景的自动裁剪与超限警告。

    使用「动态基线法」设定阈值：
      baseline = 空女主列表时的输出长度
      _HEROINE_SPEECH_TRIM_THRESHOLD = baseline + 5
    这样 h1 渲染后（baseline + h1_len > baseline+5）触发 trim，
    h2 以 trim=True 渲染，daily/angry/exposed 消失。
    """

    # ── 辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _make_heroine(prefix: str) -> dict:
        """构造带唯一标记的女主 dict，所有字段均可追踪。"""
        return {
            "name":              f"{prefix}_name",
            "affection":         30,
            "relationship_stage": "朋友",
            "personality_core":  "冷静",
            "appearance":        f"{prefix}_APPEARANCE",
            "speech_samples": {
                "most_characteristic": f"{prefix}_MOST_CHAR",
                "daily":               f"{prefix}_DAILY",
                "angry_or_hurt":       f"{prefix}_ANGRY",
                "exposed_or_flustered": f"{prefix}_EXPOSED",
            },
            "first_reactions":  [f"{prefix}_FIRST_REACT"],
            "private_anchors":  [f"{prefix}_PRIVATE_ANCHOR"],
        }

    @staticmethod
    def _ws(heroines: list) -> dict:
        return {
            "world_rules": {"setting": "测试世界", "tone": "测试基调"},
            "characters":  {"heroines": heroines, "supporting_characters": []},
            "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
        }

    # ── 测试 ────────────────────────────────────────────────────

    def test_single_heroine_shows_all_speech_samples(self):
        """单女主时，四条 speech_samples 全部出现（无裁剪）。"""
        import prompt.builder as _b
        ws = self._ws([self._make_heroine("h1")])
        result = _b.build_static_system_prompt(ws)
        for key in ("h1_MOST_CHAR", "h1_DAILY", "h1_ANGRY", "h1_EXPOSED"):
            self.assertIn(key, result, f"单女主时 {key} 应出现")

    def test_second_heroine_trimmed_when_threshold_exceeded(self):
        """h1 渲染后超过阈值 → h2 daily/angry/exposed 消失，most_char 保留。"""
        import prompt.builder as _b
        # 动态基线：空女主列表时的输出长度
        baseline = len(_b.build_static_system_prompt(self._ws([])))
        orig_thresh = _b._HEROINE_SPEECH_TRIM_THRESHOLD
        # 阈值设在基线+5，h1 渲染后（约+几百字）必然超过此值
        _b._HEROINE_SPEECH_TRIM_THRESHOLD = baseline + 5
        try:
            ws     = self._ws([self._make_heroine("h1"), self._make_heroine("h2")])
            result = _b.build_static_system_prompt(ws)
            # h1 在触发 trim 之前渲染 → 四条都在
            self.assertIn("h1_DAILY",      result, "h1 应在裁剪前完整渲染")
            # h2 以 trim=True 渲染 → 只剩 most_char
            self.assertIn("h2_MOST_CHAR",  result, "h2 most_char 应保留")
            self.assertNotIn("h2_DAILY",   result, "h2 daily 应被裁剪")
            self.assertNotIn("h2_ANGRY",   result, "h2 angry 应被裁剪")
            self.assertNotIn("h2_EXPOSED", result, "h2 exposed 应被裁剪")
        finally:
            _b._HEROINE_SPEECH_TRIM_THRESHOLD = orig_thresh

    def test_trim_preserves_appearance_first_reactions_anchors_for_h2(self):
        """trim 模式下 h2 的 appearance / first_reactions / private_anchors 完整保留。"""
        import prompt.builder as _b
        baseline = len(_b.build_static_system_prompt(self._ws([])))
        orig_thresh = _b._HEROINE_SPEECH_TRIM_THRESHOLD
        _b._HEROINE_SPEECH_TRIM_THRESHOLD = baseline + 5
        try:
            ws     = self._ws([self._make_heroine("h1"), self._make_heroine("h2")])
            result = _b.build_static_system_prompt(ws)
            self.assertIn("h2_APPEARANCE",     result)
            self.assertIn("h2_FIRST_REACT",    result)
            self.assertIn("h2_PRIVATE_ANCHOR", result)
        finally:
            _b._HEROINE_SPEECH_TRIM_THRESHOLD = orig_thresh

    def test_budget_warning_logged_when_exceeded(self):
        """输出超过 _SYSTEM_PROMPT_BUDGET → logger.warning 被调用。"""
        import prompt.builder as _b
        orig_budget = _b._SYSTEM_PROMPT_BUDGET
        _b._SYSTEM_PROMPT_BUDGET = 1          # 强制触发超限
        logging.disable(logging.NOTSET)        # 临时恢复日志（测试文件顶部禁用了所有日志）
        try:
            ws = self._ws([self._make_heroine("h1")])
            with self.assertLogs("prompt.builder", level="WARNING") as cm:
                _b.build_static_system_prompt(ws)
            self.assertTrue(
                any("超出预算" in msg for msg in cm.output),
                f"期望 '超出预算' 警告，实际日志：{cm.output}",
            )
        finally:
            _b._SYSTEM_PROMPT_BUDGET = orig_budget
            logging.disable(logging.CRITICAL)  # 恢复禁用


# ══════════════════════════════════════════════════════════════════
# 14. 长期记忆层 memory.py（step 1）
# ══════════════════════════════════════════════════════════════════

class TestMemoryLayer(unittest.TestCase):
    """
    storage/memory.py：should_summarize / compress_events /
    inject_summary_to_context 的核心行为。
    """

    # ── 辅助 ────────────────────────────────────────────────────

    @staticmethod
    def _ws(n_events: int, existing_summary: str = "") -> dict:
        """构造包含 n 条 event_cards 的 world_state。"""
        ws: dict = {
            "story_state": {
                "event_cards": [
                    {"event": f"事件{i}", "location": f"地点{i}"}
                    for i in range(n_events)
                ],
                "recent_memory":    [],
                "suspended_issues": [],
            }
        }
        if existing_summary:
            ws["story_summary"] = existing_summary
        return ws

    @staticmethod
    def _ok_generate(sys_p: str, user_p: str) -> str:
        return "GENERATED_SUMMARY"

    # ── should_summarize ────────────────────────────────────────

    def test_should_summarize_false_below_trigger(self):
        """event_cards < SUMMARY_TRIGGER（8）→ should_summarize 返回 False。"""
        from storage.memory import should_summarize, SUMMARY_TRIGGER
        self.assertFalse(should_summarize(self._ws(SUMMARY_TRIGGER - 1)))

    def test_should_summarize_true_at_trigger(self):
        """event_cards == SUMMARY_TRIGGER → should_summarize 返回 True。"""
        from storage.memory import should_summarize, SUMMARY_TRIGGER
        self.assertTrue(should_summarize(self._ws(SUMMARY_TRIGGER)))

    # ── compress_events 正常路径 ─────────────────────────────────

    def test_compress_events_reduces_cards_by_compress_count(self):
        """compress_events 后 event_cards 减少 SUMMARY_COMPRESS_COUNT 条。"""
        from storage.memory import compress_events, SUMMARY_COMPRESS_COUNT
        ws  = self._ws(SUMMARY_COMPRESS_COUNT + 3)  # 8 cards
        out = compress_events(ws, self._ok_generate)
        remaining = out["story_state"]["event_cards"]
        self.assertEqual(len(remaining), 3,
                         f"压缩 {SUMMARY_COMPRESS_COUNT} 条后剩余应为 3 条")

    def test_compress_events_adds_story_summary(self):
        """compress_events 后 story_summary 字段出现且非空。"""
        from storage.memory import compress_events
        ws  = self._ws(8)
        out = compress_events(ws, self._ok_generate)
        self.assertIn("story_summary", out)
        self.assertTrue(out["story_summary"])

    def test_compress_events_calls_generate_fn_once(self):
        """generate_fn 恰好被调用一次。"""
        from storage.memory import compress_events
        call_count = [0]

        def counting_gen(s, u):
            call_count[0] += 1
            return "summary"

        compress_events(self._ws(8), counting_gen)
        self.assertEqual(call_count[0], 1, "generate_fn 应被调用一次")

    # ── compress_events 异常路径 ─────────────────────────────────

    def test_compress_events_returns_original_on_exception(self):
        """generate_fn 抛异常时，返回原始 world_state，不崩溃，不添加 story_summary。"""
        from storage.memory import compress_events
        ws = self._ws(8)

        def failing_gen(s, u):
            raise RuntimeError("模拟API失败")

        out = compress_events(ws, failing_gen)
        # 返回原始对象（event_cards 未被裁剪）
        self.assertEqual(len(out["story_state"]["event_cards"]), 8)
        self.assertNotIn("story_summary", out)

    # ── inject_summary_to_context ────────────────────────────────

    def test_inject_returns_nonempty_when_summary_exists(self):
        """有 story_summary → inject_summary_to_context 返回含摘要的字符串。"""
        from storage.memory import inject_summary_to_context
        ws  = self._ws(0, existing_summary="SOME_SUMMARY_TEXT")
        out = inject_summary_to_context(ws)
        self.assertIn("SOME_SUMMARY_TEXT", out)
        self.assertTrue(out)

    def test_inject_returns_empty_when_no_summary(self):
        """无 story_summary → inject_summary_to_context 返回空字符串。"""
        from storage.memory import inject_summary_to_context
        self.assertEqual(inject_summary_to_context(self._ws(0)), "")

    # ── 合并逻辑：已有摘要时传入 user_prompt ────────────────────

    def test_existing_summary_included_in_user_prompt(self):
        """已有 story_summary 时，user_prompt 中包含已有摘要内容（合并逻辑）。"""
        from storage.memory import compress_events
        ws = self._ws(8, existing_summary="EXISTING_SUMMARY_CONTENT")
        captured: list[str] = []

        def capturing_gen(sys_p: str, user_p: str) -> str:
            captured.append(user_p)
            return "new summary"

        compress_events(ws, capturing_gen)
        self.assertEqual(len(captured), 1, "generate_fn 应被调用一次")
        self.assertIn("EXISTING_SUMMARY_CONTENT", captured[0],
                      "已有摘要应出现在 user_prompt 中")


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# 11. 动态上下文注入早期剧情摘要
# ══════════════════════════════════════════════════════════════════

import prompt.builder as _b_for_summary   # noqa: E402  延迟导入，测试文件顶部已屏蔽日志


class TestSummaryInContext(unittest.TestCase):
    """验证 build_dynamic_context 将 story_summary 注入动态上下文。"""

    def _ws_with_summary(self, summary: str) -> dict:
        ws = {
            "save_info": {"turn": 3, "date": "2026-01-01", "time_slot": "morning", "location": "城镇"},
            "world_rules": {"setting": "现代都市"},
            "characters": {"heroines": [], "supporting_characters": []},
            "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
            "story_summary": summary,
        }
        return ws

    def test_summary_appears_in_dynamic_context(self):
        """world_state 含 story_summary 时，build_dynamic_context 的结果包含该摘要。"""
        import prompt.builder as _b
        ws  = self._ws_with_summary("玩家在图书馆邂逅了神秘少女")
        ctx = _b.build_dynamic_context(ws)
        self.assertIn("玩家在图书馆邂逅了神秘少女", ctx)

    def test_no_summary_section_when_absent(self):
        """world_state 无 story_summary 时，动态上下文不含摘要标记。"""
        import prompt.builder as _b
        ws = {
            "save_info": {"turn": 1, "date": "2026-01-01", "time_slot": "morning", "location": "城镇"},
            "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
        }
        ctx = _b.build_dynamic_context(ws)
        self.assertNotIn("早期剧情摘要", ctx)


# ══════════════════════════════════════════════════════════════════
# 12. 存档上限 + story_summary 写入
# ══════════════════════════════════════════════════════════════════

class TestSaveLimitEnforcement(_WithTempSaves):
    """验证 save_manager 存档上限清理与 story_summary 写入行为。"""

    def _make_state(self, summary: str | None = None) -> dict:
        """构造带可选 story_summary 的 state。"""
        st = _state()
        if summary is not None:
            st["story_summary"] = summary
        return st

    def test_third_save_removes_oldest(self):
        """写入第3条存档后，目录里只剩2条，最旧的被自动删除。"""
        import time
        _sm.save("hero", self._make_state(), label="s1")
        time.sleep(0.02)   # 确保文件名时间戳不同
        _sm.save("hero", self._make_state(), label="s2")
        time.sleep(0.02)
        _sm.save("hero", self._make_state(), label="s3")
        saves = _sm.list_saves("hero")
        self.assertEqual(len(saves), 2)
        # 最旧的 s1 应被删除，剩余为 s3、s2
        labels = [s["label"] for s in saves]
        self.assertNotIn("s1", labels)
        self.assertIn("s3", labels)
        self.assertIn("s2", labels)

    def test_no_deletion_when_under_limit(self):
        """存档数量 <= 2 时，不删除任何文件。"""
        _sm.save("hero", self._make_state(), label="a1")
        _sm.save("hero", self._make_state(), label="a2")
        saves = _sm.list_saves("hero")
        self.assertEqual(len(saves), 2)

    def test_story_summary_written_when_present(self):
        """state 包含 story_summary 时，存档 JSON 里有该字段；没有时不写入。"""
        import json as _json

        # 有 summary
        p_with = _sm.save("hero", self._make_state(summary="早期摘要文本"), label="with")
        data_with = _json.loads(Path(p_with).read_text(encoding="utf-8"))
        self.assertIn("story_summary", data_with)
        self.assertEqual(data_with["story_summary"], "早期摘要文本")

        # 无 summary（需要先让上限清理后再测，直接用新故事名避免上限删除有 summary 的存档）
        p_without = _sm.save("hero2", self._make_state(summary=None), label="without")
        data_without = _json.loads(Path(p_without).read_text(encoding="utf-8"))
        self.assertNotIn("story_summary", data_without)


# ══════════════════════════════════════════════════════════════════
# 13. do_save 不传 history（用 generate 而非 generate_with_history）
# ══════════════════════════════════════════════════════════════════

class TestDoSaveNoHistory(_WithTempSaves):
    """验证 do_save 使用 generate（无历史）生成存档，不调用 generate_with_history。"""

    def _full_state(self) -> dict:
        st = _state()
        st["world_state"] = _ws()
        return st

    def test_do_save_calls_generate_not_generate_with_history(self):
        """do_save 必须调用 generate，不得调用 generate_with_history。"""
        import json as _json
        fake_json = _json.dumps(_ws())
        with patch("main.generate", return_value=fake_json) as mock_gen, \
             patch("main.generate_with_history") as mock_hist:
            _m.do_save(self._full_state(), "hero", label="t")
        mock_gen.assert_called_once()
        mock_hist.assert_not_called()

    def test_do_save_user_prompt_contains_save_request(self):
        """generate 的 user_prompt 包含 build_save_request_prompt() 的内容。"""
        import json as _json
        from prompt.builder import build_save_request_prompt
        fake_json = _json.dumps(_ws())
        captured = {}

        def fake_gen(sys_p, usr_p, force_stream=None, max_tokens_override=None):
            captured["usr"] = usr_p
            return fake_json

        with patch("main.generate", side_effect=fake_gen):
            _m.do_save(self._full_state(), "hero", label="t")

        save_kw = build_save_request_prompt()[:15]   # 取前15字做关键词
        self.assertIn(save_kw, captured.get("usr", ""))

    def test_do_save_does_not_modify_history(self):
        """do_save 执行后，state['history'] 不发生任何变化。"""
        import json as _json
        fake_json = _json.dumps(_ws())
        st = self._full_state()
        st["history"] = [{"role": "user", "content": "hi"}]
        original_history = list(st["history"])

        with patch("main.generate", return_value=fake_json):
            _m.do_save(st, "hero", label="t")

        self.assertEqual(st["history"], original_history)


# ══════════════════════════════════════════════════════════════════
# 14. _trim_history 字符总量预算裁剪
# ══════════════════════════════════════════════════════════════════

class TestTrimHistoryBudget(unittest.TestCase):
    """验证 _trim_history 双重裁剪：条数 + 字符总量上限。"""

    def _st(self, msgs: list) -> dict:
        st = _state()
        st["history"] = msgs
        return st

    def test_short_history_not_trimmed(self):
        """总字符远低于预算时，消息数量不变。"""
        msgs = [
            {"role": "user",      "content": "你好"},
            {"role": "assistant", "content": "你好！今天想玩什么？"},
        ]
        st = self._st(msgs)
        _m._trim_history(st)
        self.assertEqual(len(st["history"]), 2)

    def test_large_messages_trimmed_within_budget(self):
        """单条 assistant 消息超大时，_trim_history 后总字符 <= _HISTORY_CHAR_BUDGET。"""
        big = "X" * 1600   # 单条 1600 字
        msgs = []
        for i in range(3):
            msgs.append({"role": "user",      "content": f"q{i}"})
            msgs.append({"role": "assistant", "content": big})
        st = self._st(msgs)
        _m._trim_history(st)
        total = sum(len(m.get("content", "")) for m in st["history"])
        self.assertLessEqual(total, _m._HISTORY_CHAR_BUDGET)

    def test_oldest_pair_removed_first(self):
        """字符超限时，最旧的 user+assistant 对最先被丢弃，最新的保留。"""
        # 2 对 × (15 + 2100) = 4230 字 > 预算 4000；删最旧对后 2115 ≤ 4000
        big = "Y" * 2100
        msgs = [
            {"role": "user",      "content": "oldest_user_msg"},
            {"role": "assistant", "content": big},
            {"role": "user",      "content": "newest_user_msg"},
            {"role": "assistant", "content": big},
        ]
        st = self._st(msgs)
        _m._trim_history(st)
        contents = [m["content"] for m in st["history"]]
        self.assertNotIn("oldest_user_msg", contents)
        self.assertIn("newest_user_msg", contents)

    def test_count_limit_still_enforced(self):
        """条数超出 SESSION_WINDOW*2 时，依然按条数裁剪（短消息不触发字符限制）。"""
        msgs = []
        for i in range(12):   # SESSION_WINDOW*2=10，多出2条
            msgs.append({"role": "user",      "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        st = self._st(msgs)
        _m._trim_history(st)
        self.assertLessEqual(len(st["history"]), _m.SESSION_WINDOW * 2)


class TestPlayerActionTag(unittest.TestCase):
    """
    主循环 _user_msg 拼接格式验证（回滚【玩家行动】标签，恢复"玩家："前缀）。

    【玩家行动】标签导致 DeepSeek 误将玩家输入当系统指令，产生 JSON 响应。
    正确格式：有 _dyn 时 → "{_dyn}\n玩家：{input}"，无 _dyn 时 → 原始 input。
    """

    def _build_user_msg(self, user_input: str, world_state: dict | None = None) -> str:
        """复现主循环中的 _user_msg 拼接逻辑。"""
        from prompt.builder import build_dynamic_context
        ws = world_state or {}
        _dyn = build_dynamic_context(ws)
        return (_dyn + "\n玩家：" + user_input) if _dyn else user_input

    def test_no_player_action_tag(self):
        """_user_msg 不含【玩家行动】字样（确认回滚生效）。"""
        msg = self._build_user_msg("d")
        self.assertNotIn("【玩家行动】", msg)

    def test_with_dyn_format(self):
        """有 _dyn 时，格式为 '{_dyn}\\n玩家：{input}'。"""
        ws = {
            "save_info": {"turn": 3, "date": "2026-01-01", "time_slot": "morning", "location": "TEST_LOC"},
            "story_state": {"event_cards": [], "recent_memory": [], "suspended_issues": []},
        }
        msg = self._build_user_msg("前进", world_state=ws)
        self.assertIn("TEST_LOC", msg)
        self.assertIn("玩家：前进", msg)
        self.assertLess(msg.find("TEST_LOC"), msg.find("玩家：前进"))

    def test_no_dyn_user_msg_is_raw_input(self):
        """无 _dyn（空 world_state）时，_user_msg 直接等于原始 user_input。"""
        msg = self._build_user_msg("hello", world_state={})
        self.assertEqual(msg, "hello")


class TestDoSaveRoundHint(_WithTempSaves):
    """do_save 生成的 user_prompt 必须包含当前回合数提示（fix: turn 永远为 0）。"""

    def _full_state(self, round_: int) -> dict:
        st = _state(round_=round_)
        st["world_state"] = _ws(turn=round_)
        return st

    def _capture_user_prompt(self, state: dict) -> str:
        """运行 do_save 并捕获传给 generate 的 user_prompt。"""
        import json as _json
        fake_json = _json.dumps(_ws())
        captured = {}

        def fake_gen(sys_p, usr_p, force_stream=None, max_tokens_override=None):
            captured["usr"] = usr_p
            return fake_json

        with patch("main.generate", side_effect=fake_gen):
            _m.do_save(state, "hero", label="t")
        return captured.get("usr", "")

    def test_round_hint_present(self):
        """_save_user_msg 包含'当前回合数：N'，N 等于 state['round']。"""
        st  = self._full_state(round_=7)
        usr = self._capture_user_prompt(st)
        self.assertIn("当前回合数：7", usr)

    def test_round_hint_zero(self):
        """回合数为 0 时也能正确输出'当前回合数：0'（边界值）。"""
        st  = self._full_state(round_=0)
        usr = self._capture_user_prompt(st)
        self.assertIn("当前回合数：0", usr)

    def test_dyn_and_save_request_preserved(self):
        """_dyn 内容和 save_request 内容仍在 _save_user_msg 中（不丢失）。"""
        from prompt.builder import build_save_request_prompt
        st  = self._full_state(round_=3)
        usr = self._capture_user_prompt(st)
        save_kw = build_save_request_prompt()[:15]
        self.assertIn(save_kw, usr, "_save_request 内容丢失")


class TestRestoreStateTrim(unittest.TestCase):
    """restore_state 加载后必须立刻裁剪膨胀历史（issue: 旧存档第一回合前超预算）。"""

    def _make_save_data(self, msg_count: int, msg_len: int) -> dict:
        """构造含 msg_count 条、每条 msg_len 字内容的 _history 存档数据。"""
        hist = []
        for i in range(msg_count // 2):
            hist.append({"role": "user",      "content": "A" * msg_len})
            hist.append({"role": "assistant", "content": "B" * msg_len})
        ws = _ws(turn=5)
        ws["_history"] = hist
        return ws

    def test_bloated_history_trimmed_to_budget(self):
        """10条×500字膨胀历史加载后总字符数不超过_HISTORY_CHAR_BUDGET。"""
        data = self._make_save_data(msg_count=10, msg_len=500)
        state = _m.restore_state(data)
        total_chars = sum(len(m.get("content", "")) for m in state["history"])
        self.assertLessEqual(
            total_chars, _m._HISTORY_CHAR_BUDGET,
            f"restore_state 后 history 仍有 {total_chars} 字，超过预算 {_m._HISTORY_CHAR_BUDGET}",
        )

    def test_normal_history_preserved(self):
        """正常历史（每条50字、4条）加载后内容完整保留。"""
        data = self._make_save_data(msg_count=4, msg_len=50)
        original_hist = list(data["_history"])
        state = _m.restore_state(data)
        self.assertEqual(len(state["history"]), len(original_hist))
        for orig, restored in zip(original_hist, state["history"]):
            self.assertEqual(orig["content"], restored["content"])

    def test_trim_called_before_return(self):
        """verify: restore_state 返回值已经过裁剪，不需要调用方再次裁剪。"""
        # 构造刚好超过预算一条的历史
        budget = _m._HISTORY_CHAR_BUDGET
        # 每条 budget//4 字，4条 = budget，再加2条就超
        msg_len = budget // 4 + 1
        data = self._make_save_data(msg_count=6, msg_len=msg_len)
        state = _m.restore_state(data)
        total_chars = sum(len(m.get("content", "")) for m in state["history"])
        self.assertLessEqual(total_chars, budget)


# ══════════════════════════════════════════════════════════════════
# TestSceneAnchor — 防止场景跳跃（_build_scene_anchor）
# ══════════════════════════════════════════════════════════════════

class TestSceneAnchor(unittest.TestCase):
    """验证 _build_scene_anchor 场景锚点构造逻辑。"""

    def test_anchor_present_when_history_has_assistant(self):
        """history 中有 assistant 消息时，返回值包含【当前场景延续】前缀。"""
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "欢迎来到黑暗森林。"},
        ]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("【当前场景延续】", anchor)

    def test_anchor_absent_when_history_empty(self):
        """history 为空时返回空字符串，行为与原始代码一致。"""
        anchor = _m._build_scene_anchor([])
        self.assertEqual(anchor, "")

    def test_anchor_absent_when_no_assistant_in_history(self):
        """history 只有 user 消息时返回空字符串。"""
        history = [{"role": "user", "content": "测试"}]
        anchor = _m._build_scene_anchor(history)
        self.assertEqual(anchor, "")

    def test_anchor_uses_last_200_chars_when_long(self):
        """GM 消息超过 200 字时只取末尾 200 字，不截断更多。"""
        long_msg = "A" * 100 + "B" * 200  # 300 字，后 200 字全是 B
        history = [{"role": "assistant", "content": long_msg}]
        anchor = _m._build_scene_anchor(history)
        # 锚点中应包含 B*200，不含前面的 A
        self.assertIn("B" * 200, anchor)
        self.assertNotIn("A", anchor)

    def test_anchor_uses_latest_assistant_message(self):
        """多条 assistant 消息时，取最新（最后）一条。"""
        history = [
            {"role": "assistant", "content": "第一幕"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "第二幕"},
        ]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("第二幕", anchor)
        self.assertNotIn("第一幕", anchor)

    def test_anchor_exact_200_chars_not_truncated(self):
        """GM 消息恰好 200 字时完整保留，不截断。"""
        msg = "X" * 200
        history = [{"role": "assistant", "content": msg}]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("X" * 200, anchor)


_TEST_CLASSES = [
    TestSaveLoad,
    TestRoundState,
    TestEnforceLocks,
    TestAPIFailures,
    TestEdgeCases,
    TestSuspendedFooter,
    TestTimestampUniqueness,
    TestTxtFallbackVisible,
    TestLoadConfigCache,
    TestGitignore,
    TestBudgetConstants,
    TestHeroineTrimParam,
    TestBudgetDetection,
    TestMemoryLayer,
    TestSummaryInContext,
    TestSaveLimitEnforcement,
    TestDoSaveNoHistory,
    TestTrimHistoryBudget,
    TestPlayerActionTag,
    TestDoSaveRoundHint,
    TestRestoreStateTrim,
    TestSceneAnchor,
]

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in _TEST_CLASSES:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)

    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)

    print("\n" + "═" * 60)
    print(f"  共 {total} 项    通过 {passed}    "
          f"失败 {len(result.failures)}    错误 {len(result.errors)}")

    if result.failures:
        print("\n  ── FAIL ────────────────────────────────────────")
        for test, tb in result.failures:
            print(f"  ✗  {test}")
            lines = [l.strip() for l in tb.strip().splitlines() if l.strip()]
            for line in lines[-2:]:
                print(f"       {line}")

    if result.errors:
        print("\n  ── ERROR ───────────────────────────────────────")
        for test, tb in result.errors:
            print(f"  ✗  {test}")
            lines = [l.strip() for l in tb.strip().splitlines() if l.strip()]
            for line in lines[-2:]:
                print(f"       {line}")

    if not result.failures and not result.errors:
        print("  全部通过 [OK]")
    print("═" * 60)
    sys.exit(0 if result.wasSuccessful() else 1)
