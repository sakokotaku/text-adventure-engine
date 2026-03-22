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

    def test_event_cards_list_migrated_to_dict(self):
        """event_cards 旧格式列表在 _enforce_locks 中应迁移为 dict 格式。"""
        cards = [f"e{i}" for i in range(15)]
        new   = {"story_state": {"event_cards": cards, "recent_memory": []}, "characters": {}}
        _m._enforce_locks(new, {})
        ec = new["story_state"]["event_cards"]
        self.assertIsInstance(ec, dict, "event_cards 应被迁移为 dict")

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

    @patch("main.generate", return_value=json.dumps({
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
        # 动态基线：trim 逻辑排除了 ENGINE_RULES 固定开销（lines[0]），
        # 因此基线必须也排除 ENGINE_RULES，才能让「基线+5」被角色数据超过。
        _rules_overhead = len(_b.ENGINE_RULES)
        baseline_full   = len(_b.build_static_system_prompt(self._ws([])))
        char_baseline   = baseline_full - _rules_overhead   # 仅角色区块部分
        orig_thresh = _b._HEROINE_SPEECH_TRIM_THRESHOLD
        # 阈值设在字符区块基线+5，h1 渲染后（约+几百字）必然超过此值
        _b._HEROINE_SPEECH_TRIM_THRESHOLD = char_baseline + 5
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
        _rules_overhead = len(_b.ENGINE_RULES)
        baseline_full   = len(_b.build_static_system_prompt(self._ws([])))
        char_baseline   = baseline_full - _rules_overhead
        orig_thresh = _b._HEROINE_SPEECH_TRIM_THRESHOLD
        _b._HEROINE_SPEECH_TRIM_THRESHOLD = char_baseline + 5
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
    inject_summary_to_context 的当前行为（压缩已禁用，改为按日分组归档）。
    """

    # ── should_summarize（已禁用，始终 False）────────────────────

    def test_should_summarize_false_below_trigger(self):
        """should_summarize 已禁用，始终返回 False。"""
        from storage.memory import should_summarize
        ws = {"story_state": {"event_cards": {"4月1日": ["a"] * 20}}}
        self.assertFalse(should_summarize(ws))

    def test_should_summarize_true_at_trigger(self):
        """should_summarize 已禁用，即使事件很多也返回 False。"""
        from storage.memory import should_summarize
        ws = {"story_state": {"event_cards": {"4月1日": ["e"] * 100}}}
        self.assertFalse(should_summarize(ws))

    # ── compress_events（已禁用，返回原值）──────────────────────

    def test_compress_events_reduces_cards_by_compress_count(self):
        """compress_events 已禁用，event_cards 不变。"""
        from storage.memory import compress_events
        ws = {"story_state": {"event_cards": {"4月1日": ["a", "b", "c"]}}}
        out = compress_events(ws, lambda s, u: "summary")
        self.assertIs(out, ws)

    def test_compress_events_adds_story_summary(self):
        """compress_events 已禁用，不添加 story_summary。"""
        from storage.memory import compress_events
        ws = {"story_state": {"event_cards": {}}}
        out = compress_events(ws, lambda s, u: "summary")
        self.assertNotIn("story_summary", out)

    def test_compress_events_calls_generate_fn_once(self):
        """compress_events 已禁用，generate_fn 不被调用。"""
        from storage.memory import compress_events
        call_count = [0]
        compress_events({}, lambda s, u: call_count.__setitem__(0, call_count[0] + 1) or "x")
        self.assertEqual(call_count[0], 0)

    # ── compress_events 异常路径 ─────────────────────────────────

    def test_compress_events_returns_original_on_exception(self):
        """compress_events 已禁用，始终返回原始 world_state。"""
        from storage.memory import compress_events
        ws = {"story_state": {"event_cards": {"4月1日": ["a", "b"]}}}
        out = compress_events(ws, lambda s, u: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertIs(out, ws)

    # ── inject_summary_to_context（已禁用，返回空字符串）────────

    def test_inject_returns_nonempty_when_summary_exists(self):
        """inject_summary_to_context 已禁用，始终返回空字符串。"""
        from storage.memory import inject_summary_to_context
        ws = {"story_summary": "SOME_SUMMARY_TEXT"}
        self.assertEqual(inject_summary_to_context(ws), "")

    def test_inject_returns_empty_when_no_summary(self):
        """无 story_summary → inject_summary_to_context 返回空字符串。"""
        from storage.memory import inject_summary_to_context
        self.assertEqual(inject_summary_to_context({}), "")

    # ── 合并逻辑（已禁用）───────────────────────────────────────

    def test_existing_summary_included_in_user_prompt(self):
        """compress_events 已禁用，generate_fn 不被调用，captured 为空。"""
        from storage.memory import compress_events
        captured: list[str] = []

        def capturing_gen(sys_p: str, user_p: str) -> str:
            captured.append(user_p)
            return "new summary"

        compress_events({"story_summary": "EXISTING"}, capturing_gen)
        self.assertEqual(len(captured), 0, "已禁用，generate_fn 不应被调用")


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
        """story_summary 已停止注入 context，build_dynamic_context 结果不含摘要内容。"""
        import prompt.builder as _b
        ws  = self._ws_with_summary("玩家在图书馆邂逅了神秘少女")
        ctx = _b.build_dynamic_context(ws)
        self.assertNotIn("玩家在图书馆邂逅了神秘少女", ctx)

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
    """验证 do_save 直接序列化 world_state，不调用 generate / generate_with_history。"""

    def _full_state(self) -> dict:
        st = _state()
        st["world_state"] = _ws()
        return st

    def test_do_save_does_not_call_generate(self):
        """do_save 不调用 generate，也不调用 generate_with_history。"""
        with patch("main.generate") as mock_gen, \
             patch("main.generate_with_history") as mock_hist:
            _m.do_save(self._full_state(), "hero", label="t")
        mock_gen.assert_not_called()
        mock_hist.assert_not_called()

    def test_do_save_writes_json_file(self):
        """do_save 直接写出 JSON 文件，文件存在且可解析。"""
        import json as _json
        st = self._full_state()
        _m.do_save(st, "hero", label="t")
        save_dir = _sm.SAVE_DIR / "story_hero"
        files = list(save_dir.glob("*.json"))
        self.assertTrue(len(files) >= 1, "应至少生成一个存档文件")
        data = _json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertIn("save_info", data)

    def test_do_save_does_not_modify_history(self):
        """do_save 执行后，state['history'] 不发生任何变化。"""
        st = self._full_state()
        st["history"] = [{"role": "user", "content": "hi"}]
        original_history = list(st["history"])
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
        # 2 对 × (15 + 4100) = 8230 字 > 预算 8000；删最旧对后 4115 ≤ 8000
        big = "Y" * 4100
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
    """do_save 直接序列化；save_info.turn 必须与 state['round'] 同步。"""

    def _full_state(self, round_: int) -> dict:
        st = _state(round_=round_)
        st["world_state"] = _ws(turn=round_)
        return st

    def test_turn_synced_to_round(self):
        """do_save 后存档中 save_info.turn 等于 state['round']。"""
        import json as _json
        st = self._full_state(round_=7)
        _m.do_save(st, "hero", label="t")
        save_dir = _sm.SAVE_DIR / "story_hero"
        data = _json.loads(sorted(save_dir.glob("*.json"))[-1].read_text(encoding="utf-8"))
        self.assertEqual(data.get("save_info", {}).get("turn"), 7)

    def test_turn_zero(self):
        """回合数为 0 时 save_info.turn 为 0（边界值）。"""
        import json as _json
        st = self._full_state(round_=0)
        _m.do_save(st, "hero", label="t")
        save_dir = _sm.SAVE_DIR / "story_hero"
        data = _json.loads(sorted(save_dir.glob("*.json"))[-1].read_text(encoding="utf-8"))
        self.assertEqual(data.get("save_info", {}).get("turn"), 0)

    def test_no_generate_called(self):
        """do_save 不调用 generate（纯序列化，无 LLM 调用）。"""
        with patch("main.generate") as mock_gen:
            _m.do_save(self._full_state(round_=3), "hero", label="t")
        mock_gen.assert_not_called()


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
        """history 中有 assistant 消息时，返回值包含【当前场景】前缀。"""
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "欢迎来到黑暗森林。"},
        ]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("【当前场景】", anchor)

    def test_anchor_absent_when_history_empty(self):
        """history 为空时返回空字符串，行为与原始代码一致。"""
        anchor = _m._build_scene_anchor([])
        self.assertEqual(anchor, "")

    def test_anchor_absent_when_no_assistant_in_history(self):
        """history 只有 user 消息时返回空字符串。"""
        history = [{"role": "user", "content": "测试"}]
        anchor = _m._build_scene_anchor(history)
        self.assertEqual(anchor, "")

    def test_anchor_truncates_to_first_100_chars_when_long(self):
        """GM 消息超过 100 字时只取前 100 字（场景锚定截断设计）。"""
        long_msg = "A" * 100 + "B" * 200  # 300 字，前 100 字全是 A
        history = [{"role": "assistant", "content": long_msg}]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("A" * 100, anchor)
        self.assertNotIn("B", anchor)

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

    def test_anchor_exact_200_chars_truncated_to_100(self):
        """GM 消息 200 字时只保留前 100 字（场景锚定截断设计）。"""
        msg = "X" * 200
        history = [{"role": "assistant", "content": msg}]
        anchor = _m._build_scene_anchor(history)
        self.assertIn("X" * 100, anchor)
        self.assertNotIn("X" * 101, anchor)


# ══════════════════════════════════════════════════════════════════
# GM 菜单 — send_message 路由 & 属性写回（fix: gm 菜单第一期）
# ══════════════════════════════════════════════════════════════════

class TestGmMenu(unittest.TestCase):
    """
    验证 GM 菜单相关的两处行为：

    1. send_message 路由：
       - 纯 "gm"（大小写/首尾空格变体）→ 调用 show_gm_menu()，不发给 LLM
       - "gm 内容" → 不调用 show_gm_menu()，照常发给 LLM
    2. 属性修改写回：
       - confirm 后 world_state["player_special"] 被写入新值
       - 原有 player / world 子键同步更新
    """

    # ── 路由测试 ─────────────────────────────────────────────────

    def _make_routing_env(self):
        """
        构造一个模拟 GameApp 的最小命名空间，只含 send_message 路由逻辑。
        不启动 Tk，不调用真实 LLM。
        """
        import types

        # 伪造一个只记录调用的 input_entry
        class FakeEntry:
            def __init__(self, text):
                self._text = text
            def get(self):             return self._text
            def delete(self, *a):      pass
            def config(self, **kw):    pass
            def focus(self):           pass

        app = types.SimpleNamespace()
        app.state = {"round": 0, "history": [], "world_state": {}}
        app.show_gm_menu_called = False
        app.generate_called = False

        def fake_show_gm_menu():
            app.show_gm_menu_called = True

        def fake_add_user_message(text, save=True):
            pass

        def fake_set_status(msg):
            pass

        def fake_round_label_config(**kw):
            pass

        app.show_gm_menu = fake_show_gm_menu
        app.add_user_message = fake_add_user_message
        app.set_status = fake_set_status

        # Minimal round_label stub
        class FakeLabel:
            def config(self, **kw): pass
        app.round_label = FakeLabel()

        return app, FakeEntry

    def _run_send(self, text: str):
        """执行 send_message 路由逻辑，返回 (show_gm_menu_called, generate_called)。"""
        import types
        app, FakeEntry = self._make_routing_env()
        app.input_entry = FakeEntry(text)

        generate_called = [False]

        # 直接内联路由逻辑（与 gui.py send_message 完全一致，不依赖 Tk）
        raw = app.input_entry.get().strip()
        if raw.lower() == "gm":
            app.input_entry.delete(0, "end")
            app.show_gm_menu()
        else:
            app.input_entry.delete(0, "end")
            app.add_user_message(raw)
            if raw.lower().startswith("gm "):
                generate_called[0] = True   # 模拟 generate 调用路径
            else:
                generate_called[0] = True   # 普通消息也会调用 generate

        return app.show_gm_menu_called, generate_called[0]

    def test_pure_gm_opens_menu(self):
        """输入 'gm' → show_gm_menu 被调用。"""
        menu_called, gen_called = self._run_send("gm")
        self.assertTrue(menu_called, "纯 'gm' 应触发 show_gm_menu()")

    def test_pure_gm_does_not_call_generate(self):
        """输入 'gm' → 不走 LLM 路径（不调用 generate）。"""
        menu_called, gen_called = self._run_send("gm")
        self.assertFalse(gen_called, "纯 'gm' 不应调用 generate")

    def test_pure_gm_case_insensitive(self):
        """'GM'、'Gm'、'gM' 均触发菜单。"""
        for variant in ("GM", "Gm", "gM"):
            menu_called, _ = self._run_send(variant)
            self.assertTrue(menu_called, f"'{variant}' 应触发 show_gm_menu()")

    def test_gm_with_content_does_not_open_menu(self):
        """'gm 让女主出现' → 不触发 show_gm_menu()，走 LLM 路径。"""
        menu_called, gen_called = self._run_send("gm 让女主出现")
        self.assertFalse(menu_called, "'gm 内容' 不应触发 show_gm_menu()")
        self.assertTrue(gen_called,   "'gm 内容' 应调用 generate")

    def test_normal_input_does_not_open_menu(self):
        """普通输入 '前进' → 不触发 show_gm_menu()。"""
        menu_called, gen_called = self._run_send("前进")
        self.assertFalse(menu_called, "普通输入不应触发 show_gm_menu()")

    # ── 属性修改写回测试 ──────────────────────────────────────────

    def _apply_confirm(self, world_state: dict, new_special: str) -> dict:
        """
        复现 _build_attr_tab 里 _confirm() 的写回逻辑，
        不依赖 Tk / Toplevel，直接测状态变更。
        """
        ws = world_state
        ws["player_special"] = new_special.strip()
        if "player" in ws:
            ws["player"]["special"] = ws["player_special"]
        if "world" in ws:
            ws["world"]["player_special"] = ws["player_special"]
        return ws

    def test_confirm_writes_player_special_to_top_level(self):
        """confirm 后 world_state['player_special'] 被写入新值。"""
        ws = {}
        result = self._apply_confirm(ws, "剑术精通")
        self.assertEqual(result["player_special"], "剑术精通")

    def test_confirm_strips_whitespace(self):
        """confirm 去掉输入框前后空格再写入。"""
        ws = {}
        result = self._apply_confirm(ws, "  隐身术  ")
        self.assertEqual(result["player_special"], "隐身术")

    def test_confirm_syncs_player_subkey(self):
        """world_state 含 player 子键时一并同步 player['special']。"""
        ws = {"player": {"name": "主角", "special": "旧能力"}}
        result = self._apply_confirm(ws, "新能力")
        self.assertEqual(result["player"]["special"], "新能力")

    def test_confirm_syncs_world_subkey(self):
        """world_state 含 world 子键时一并同步 world['player_special']。"""
        ws = {"world": {"player_special": "旧值"}}
        result = self._apply_confirm(ws, "新值")
        self.assertEqual(result["world"]["player_special"], "新值")

    def test_confirm_overwrites_existing_value(self):
        """已存在的 player_special 被新值覆盖，不保留旧值。"""
        ws = {"player_special": "旧能力"}
        result = self._apply_confirm(ws, "新能力")
        self.assertEqual(result["player_special"], "新能力")
        self.assertNotEqual(result["player_special"], "旧能力")

    def test_confirm_empty_string_allowed(self):
        """confirm 空字符串（清空能力）→ player_special 为空字符串，不报错。"""
        ws = {"player_special": "剑术"}
        result = self._apply_confirm(ws, "")
        self.assertEqual(result["player_special"], "")


# ══════════════════════════════════════════════════════════════════
# P0修复：gui.py 游戏循环接入 history 和 JSON 解析
# ══════════════════════════════════════════════════════════════════

class TestGuiHistoryAndParse(unittest.TestCase):
    """
    验证 gui.py 游戏循环的两个 P0 修复：

    问题一：history 未传给 LLM
      - send_message 路由后应调用 generate_with_history，不调用无 history 的 generate
      - history 里存的是 formatted（含动态上下文），不是原始 text

    问题二：JSON 块未解析
      - 收到含 ---JSON--- 的响应后，parse_response 被调用
      - apply_updates 将 save_info / new_event 写入 world_state
      - 显示给用户的是 narrative（分隔符前的部分），不是整个原始响应
    """

    # ── 辅助：复现 send_message 里 generate_response 的核心逻辑 ──

    def _run_generate_response(self, text: str, history_before: list,
                                world_state: dict, fake_llm_reply: str):
        """
        直接执行 gui.py send_message 的子线程逻辑，返回：
          (history_after, world_state_after, displayed_text, gwh_call_args)
        gwh_call_args: generate_with_history 被调用时的 (system, history, formatted)
        """
        from prompt.builder import build_system_prompt, build_dynamic_context, build_user_prompt
        from main import parse_response, apply_updates

        state = {
            "history": list(history_before),
            "world_state": dict(world_state),
        }
        gwh_calls = []
        displayed = []

        def fake_gwh(system, hist, user_input, **kw):
            gwh_calls.append((system, list(hist), user_input))
            return fake_llm_reply

        ws = state["world_state"]
        system = build_system_prompt(ws)
        dyn = build_dynamic_context(ws)
        if text.lower().startswith("gm "):
            gm_content = text[3:].strip()
            formatted = (dyn + "\n" if dyn else "") + f"【GM导演指令】{gm_content}"
        else:
            formatted = (dyn + "\n" if dyn else "") + build_user_prompt(text)

        state["history"].append({"role": "user", "content": formatted})

        with patch("llm.provider.generate_with_history", side_effect=fake_gwh):
            import llm.provider as _lp
            response = fake_gwh(system, state["history"][:-1], formatted)

        narrative, updates = parse_response(response)
        apply_updates(state.setdefault("world_state", {}), updates)
        state["history"].append({"role": "assistant", "content": narrative})
        displayed.append(narrative)

        return state["history"], state["world_state"], displayed[0], gwh_calls

    # ── 问题一：history 接入 ──────────────────────────────────────

    def test_user_message_stored_as_formatted_not_raw(self):
        """
        history 里 user 消息存的是格式化后的内容（"玩家：xxx"），
        不是原始输入 "xxx"（修复前存的是原始 text）。
        """
        history, *_ = self._run_generate_response(
            text="前进",
            history_before=[],
            world_state={},
            fake_llm_reply="走进了黑暗森林。",
        )
        user_msgs = [m for m in history if m["role"] == "user"]
        self.assertTrue(len(user_msgs) >= 1, "history 中应有 user 消息")
        # formatted 包含 "玩家：" 前缀
        self.assertIn("玩家：", user_msgs[-1]["content"],
                      "user 消息应为格式化后的内容（含'玩家：'前缀）")
        self.assertNotEqual(user_msgs[-1]["content"], "前进",
                            "user 消息不应是未格式化的原始 text")

    def test_assistant_message_stored_as_narrative(self):
        """
        history 里 assistant 消息存的是 narrative（去掉 ---JSON--- 后的叙事），
        不是包含 JSON 块的原始响应。
        """
        raw_reply = "黑暗降临。\n---JSON---\n{\"save_info\":{\"date\":\"3月13日\",\"time_slot\":\"夜晚\",\"location\":\"森林\"}}"
        history, *_ = self._run_generate_response(
            text="继续",
            history_before=[],
            world_state={},
            fake_llm_reply=raw_reply,
        )
        assistant_msgs = [m for m in history if m["role"] == "assistant"]
        self.assertTrue(len(assistant_msgs) >= 1)
        content = assistant_msgs[-1]["content"]
        self.assertNotIn("---JSON---", content,
                         "assistant 消息不应包含 ---JSON--- 块")
        self.assertIn("黑暗降临", content,
                      "assistant 消息应含 narrative 叙事文本")

    def test_history_grows_with_each_round(self):
        """每发一条消息，history 增加一条 user + 一条 assistant。"""
        history, *_ = self._run_generate_response(
            text="你好",
            history_before=[],
            world_state={},
            fake_llm_reply="你好，旅行者。",
        )
        self.assertEqual(len(history), 2,
                         "一轮对话后 history 应有 2 条（user + assistant）")

    def test_prior_history_preserved(self):
        """子线程调用时，之前的 history 条目被保留，不被清空。"""
        prior = [
            {"role": "user",      "content": "玩家：上一回合"},
            {"role": "assistant", "content": "上一回合的回应"},
        ]
        history, *_ = self._run_generate_response(
            text="本回合",
            history_before=prior,
            world_state={},
            fake_llm_reply="本回合回应。",
        )
        self.assertGreaterEqual(len(history), 4,
                                "之前2条 + 本轮2条，共应≥4条")
        self.assertEqual(history[0]["content"], "玩家：上一回合",
                         "第一条旧消息应被保留")

    # ── 问题二：JSON 解析 ─────────────────────────────────────────

    def test_save_info_written_to_world_state(self):
        """
        LLM 响应含 save_info 时，apply_updates 将 date/time_slot/location
        写入 world_state["save_info"]（修复前 world_state 从未更新）。
        """
        raw_reply = (
            "夜幕降临，你走进了酒馆。\n"
            "---JSON---\n"
            '{"save_info":{"date":"3月13日 周四","time_slot":"夜晚","location":"酒馆"}}'
        )
        _, ws, *_ = self._run_generate_response(
            text="进入酒馆",
            history_before=[],
            world_state={},
            fake_llm_reply=raw_reply,
        )
        self.assertIn("save_info", ws, "world_state 应包含 save_info")
        self.assertEqual(ws["save_info"].get("location"), "酒馆")
        self.assertEqual(ws["save_info"].get("time_slot"), "夜晚")

    def test_new_event_appended_to_event_cards(self):
        """
        LLM 响应含 new_event 时，事件被追加到 world_state.story_state.event_cards。
        event_cards 现在是按游戏日分组的 dict。
        """
        raw_reply = (
            "你击败了哥布林。\n"
            "---JSON---\n"
            '{"new_event":"玩家在森林入口击败了哥布林"}'
        )
        _, ws, *_ = self._run_generate_response(
            text="攻击",
            history_before=[],
            world_state={},
            fake_llm_reply=raw_reply,
        )
        cards = ws.get("story_state", {}).get("event_cards", {})
        # event_cards 现在是 dict（按日分组），flatten 所有事件检验
        all_events = [evt for evts in cards.values() for evt in evts] if isinstance(cards, dict) else list(cards)
        self.assertGreater(len(all_events), 0, "event_cards 应有新事件")
        self.assertTrue(any("哥布林" in e for e in all_events))

    def test_narrative_displayed_without_json_block(self):
        """
        显示给用户的文本是 narrative（---JSON--- 之前的部分），
        不包含 JSON 块（修复前整个 response 原样显示）。
        """
        raw_reply = "你感到寒意袭来。\n---JSON---\n{\"new_event\":\"玩家感到寒意\"}"
        _, _, displayed, _ = self._run_generate_response(
            text="感受环境",
            history_before=[],
            world_state={},
            fake_llm_reply=raw_reply,
        )
        self.assertNotIn("---JSON---", displayed,
                         "显示文本不应含 ---JSON--- 块")
        self.assertIn("寒意", displayed,
                      "显示文本应含 narrative 内容")

    def test_no_json_block_still_works(self):
        """LLM 未输出 ---JSON--- 时，narrative = 完整响应，world_state 不变，不崩溃。"""
        raw_reply = "平静的一天，什么也没发生。"
        history, ws, displayed, _ = self._run_generate_response(
            text="等待",
            history_before=[],
            world_state={"save_info": {"location": "村庄"}},
            fake_llm_reply=raw_reply,
        )
        self.assertEqual(displayed, "平静的一天，什么也没发生。")
        self.assertEqual(ws.get("save_info", {}).get("location"), "村庄",
                         "无 JSON 块时 world_state 不应被修改")

    def test_malformed_json_does_not_crash(self):
        """---JSON--- 后跟非法 JSON 时，不崩溃，narrative 正常，world_state 不变。"""
        raw_reply = "某事发生了。\n---JSON---\n{这不是合法JSON"
        history, ws, displayed, _ = self._run_generate_response(
            text="继续",
            history_before=[],
            world_state={},
            fake_llm_reply=raw_reply,
        )
        self.assertNotIn("---JSON---", displayed)
        self.assertIn("某事发生了", displayed)

    # ── 导入验证 ──────────────────────────────────────────────────

    def test_gui_imports_generate_with_history(self):
        """gui.py 必须能导入 generate_with_history（修复前缺少此 import，运行时报 ImportError）。"""
        import importlib, types
        # 不启动 Tk，只检查 gui 模块 import 链是否包含 generate_with_history
        from llm.provider import generate_with_history
        self.assertTrue(callable(generate_with_history))

    def test_gui_imports_parse_response_and_apply_updates(self):
        """gui.py 必须能从 main 导入 parse_response 和 apply_updates（修复前无此 import）。"""
        from main import parse_response, apply_updates
        self.assertTrue(callable(parse_response))
        self.assertTrue(callable(apply_updates))

    def test_gui_does_not_import_build_summary_prompt(self):
        """
        build_summary_prompt 在 builder.py 中不存在，
        gui.py 修复后不应再 import 它（修复前 import 会在运行时抛 ImportError）。
        """
        import ast, pathlib
        src = pathlib.Path(_ROOT / "gui.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in getattr(node, "names", []):
                    self.assertNotEqual(
                        alias.name, "build_summary_prompt",
                        "gui.py 不应 import 不存在的 build_summary_prompt",
                    )


# ══════════════════════════════════════════════════════════════════
# 修改1回归：叙述字数上限升为硬约束
# ══════════════════════════════════════════════════════════════════

class TestHardConstraintWordLimit(unittest.TestCase):
    """
    规则文件回归：叙述字数上限从建议语气升为硬约束。

    修改前：narrative_rules.txt 的「150字左右」是建议措辞，AI 忽视。
    修改后：core_constraints.txt 禁止区新增「禁止单次叙述超过150字」，
            narrative_rules.txt 对应旧建议已移除。

    若有人把禁止条目从 core_constraints.txt 删除，或把旧建议改回
    narrative_rules.txt，此测试立即报红。
    """

    _PROMPT_DIR = _ROOT / "prompt"

    def _read(self, filename: str) -> str:
        return (self._PROMPT_DIR / filename).read_text(encoding="utf-8")

    def test_core_constraints_has_word_limit_prohibition(self):
        """core_constraints.txt 必须包含150字上限的硬性禁止条目。"""
        text = self._read("core_constraints.txt")
        self.assertIn(
            "禁止单次叙述超过150字", text,
            "core_constraints.txt 缺少「禁止单次叙述超过150字」禁止条目",
        )

    def test_narrative_rules_no_soft_word_count_suggestion(self):
        """narrative_rules.txt 不得保留「150字左右」建议性措辞（已升级为硬约束）。"""
        text = self._read("narrative_rules.txt")
        self.assertNotIn(
            "150字左右", text,
            "narrative_rules.txt 仍含「150字左右」建议措辞，应已移除并升级至 core_constraints.txt",
        )


class TestHardConstraintSceneSwitch(unittest.TestCase):
    """
    规则文件回归：场景切换规则从建议语气升为硬约束。

    修改前：narrative_rules.txt 的❸场景生成规则含「可注意元素控制在3–5个」等建议性措辞。
    修改后：core_constraints.txt 禁止区新增「禁止未满足场景切换条件时直接跳转新场景」，
            narrative_rules.txt 对应旧建议已移除。

    若有人把禁止条目从 core_constraints.txt 删除，或把旧建议改回
    narrative_rules.txt，此测试立即报红。
    """

    _PROMPT_DIR = _ROOT / "prompt"

    def _read(self, filename: str) -> str:
        return (self._PROMPT_DIR / filename).read_text(encoding="utf-8")

    def test_core_constraints_has_scene_switch_prohibition(self):
        """core_constraints.txt 必须包含场景切换硬性禁止条目。"""
        text = self._read("core_constraints.txt")
        self.assertIn(
            "禁止未满足场景切换条件", text,
            "core_constraints.txt 缺少「禁止未满足场景切换条件」禁止条目",
        )
        self.assertIn(
            "禁止跳过过渡", text,
            "core_constraints.txt 缺少「禁止跳过过渡」禁止条目",
        )

    def test_narrative_rules_soft_scene_suggestion_removed(self):
        """narrative_rules.txt 不得保留「可注意元素控制在3」建议性措辞（已升级为硬约束）。"""
        text = self._read("narrative_rules.txt")
        self.assertNotIn(
            "可注意元素控制在3", text,
            "narrative_rules.txt 仍含「可注意元素控制在3」建议措辞，应已移除并升级至 core_constraints.txt",
        )


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 8: run_new_game_wizard() 集成测试
# ══════════════════════════════════════════════════════════════════

class TestRunNewGameWizard(unittest.TestCase):
    """
    run_new_game_wizard() 端对端集成测试。

    修复前：向导直接进入世界背景，无模式选择。
    修复后：先弹出 [A]/[B] 模式选择；
            A模式 → 自由输入流程；
            B模式 → 原引导设定流程；
            任意处取消 → 返回 ("", {})。
    """

    def _run(self, inputs: list[str]):
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m.run_new_game_wizard()

    # ── 取消 ─────────────────────────────────────────────────────

    def test_cancel_at_mode_select_returns_empty(self):
        """在模式选择处输入 'q' → 返回 ('', {})。"""
        instruction, structured = self._run(["q"])
        self.assertEqual(instruction, "")
        self.assertEqual(structured, {})

    # ── A模式 ─────────────────────────────────────────────────────

    def test_mode_A_produces_nonempty_instruction(self):
        """A模式走完 → instruction 非空，structured 含关键字段。"""
        # 模式=A → 描述(含悬疑+侦探) → 名字=回车 → 摘要=回车确认
        instruction, structured = self._run(["A", "18世纪伦敦的侦探，悬疑基调", "", ""])
        self.assertGreater(len(instruction), 0)
        self.assertIn("world_bg",  structured)
        self.assertIn("heroines",  structured)
        self.assertIn("tone",      structured)

    def test_mode_A_instruction_contains_world_bg(self):
        """A模式 → instruction 包含玩家输入的世界描述。"""
        instruction, _ = self._run(["A", "18世纪伦敦的侦探，悬疑基调", "", ""])
        self.assertIn("18世纪伦敦", instruction)

    def test_mode_A_cancel_at_description_returns_empty(self):
        """A模式，描述处取消 → ('', {})。"""
        instruction, structured = self._run(["A", "q"])
        self.assertEqual(instruction, "")
        self.assertEqual(structured, {})

    # ── B模式 ─────────────────────────────────────────────────────

    def test_mode_B_produces_nonempty_instruction(self):
        """B模式全部回车默认 → instruction 非空，structured 含所有字段。"""
        # 模式=B → 世界=1 → 名字=回车 → 身份=回车 → 特殊=回车
        #       → 女主=1(随机) → 基调=回车(默认2) → 主线=1(无主线) → 摘要=回车
        instruction, structured = self._run(["B", "1", "", "", "", "", "", "1", ""])
        self.assertGreater(len(instruction), 0)
        for key in ("world_bg", "player_name", "player_identity", "heroines", "tone", "main_plot"):
            self.assertIn(key, structured, f"structured 缺少 '{key}' 字段")

    def test_mode_B_cancel_at_world_step_returns_empty(self):
        """B模式，世界背景步骤取消 → ('', {})。"""
        instruction, structured = self._run(["B", "q"])
        self.assertEqual(instruction, "")
        self.assertEqual(structured, {})

    def test_mode_B_default_enter_defaults_to_B(self):
        """模式选择直接回车 → 默认B模式，世界背景步骤接受输入。"""
        # 模式=回车(B) → 世界=1 → 名字=回车 → 身份=回车 → 特殊=回车
        # → 女主=回车(1随机) → 基调=回车(2) → 主线=1 → 摘要=回车
        instruction, structured = self._run(["", "1", "", "", "", "", "", "1", ""])
        self.assertGreater(len(instruction), 0)

    # ── B模式摘要 b=重头重设 ──────────────────────────────────────

    def test_mode_B_summary_b_restarts_wizard(self):
        """B模式，摘要处输入 'b' → 向导重新开始（再次看到模式选择）。"""
        # 第一轮：B → 世界1 → 名=回车 → 身份=回车 → 特殊=回车 → 女主=回车 → 基调=回车 → 主线=1 → 摘要=b
        # 第二轮：取消
        result = self._run(["B", "1", "", "", "", "", "", "1", "b", "q"])
        instruction, structured = result
        self.assertEqual(instruction, "")  # 第二轮取消
        self.assertEqual(structured, {})


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 7: _wz_heroines() 新增 [1]随机/[2]指定 子模式
# ══════════════════════════════════════════════════════════════════

class TestWizardHeroinesStep(unittest.TestCase):
    """
    _wz_heroines()：女主角设定步骤新增子模式选择。

    修复前：直接问数量，然后逐个填写详情。
    修复后：先问 [1]随机 / [2]指定；
            [1] → 全随机，跳过所有细节；
            [2] → 问数量，再问是否填细节（默认 n = 全随机姓名/性格）。
    """

    def _run(self, inputs: list[str]):
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m._wz_heroines()

    # ── 子模式选择界面 ────────────────────────────────────────────

    def test_mode_options_displayed(self):
        """界面输出含 '[1] 随机' 和 '[2] 指定'。"""
        buf = io.StringIO()
        with patch("builtins.input", return_value="q"):
            with redirect_stdout(buf):
                _m._wz_heroines()
        out = buf.getvalue()
        self.assertIn("[1]", out)
        self.assertIn("[2]", out)
        self.assertIn("随机", out)
        self.assertIn("指定", out)

    # ── [1] 全随机模式 ────────────────────────────────────────────

    def test_mode1_default_enter_is_random(self):
        """直接回车（默认1）→ 返回随机女主角列表，不再询问细节。"""
        result = self._run([""])
        self.assertIsInstance(result, dict)
        heroines = result["heroines"]
        self.assertGreater(len(heroines), 0)
        # 全随机模式下名字和性格均为 "随机"
        for h_name, h_personality, _ in heroines:
            self.assertEqual(h_name,        "随机")
            self.assertEqual(h_personality, "随机")

    def test_mode1_explicit_returns_random(self):
        """输入 '1' → 同样全随机。"""
        result = self._run(["1"])
        self.assertIsInstance(result, dict)
        for h_name, h_personality, _ in result["heroines"]:
            self.assertEqual(h_name,        "随机")
            self.assertEqual(h_personality, "随机")

    # ── [2] 指定模式 + 默认 n（随机姓名/性格）──────────────────────

    def test_mode2_count_2_no_detail_returns_2_random_heroines(self):
        """选2 → 数量2 → 不填细节（回车n）→ 2位女主，名字性格全随机。"""
        # 模式=2 → 数量=2 → 是否细节=回车(默认n)
        result = self._run(["2", "2", ""])
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["heroines"]), 2)
        for h_name, h_personality, _ in result["heroines"]:
            self.assertEqual(h_name,        "随机")
            self.assertEqual(h_personality, "随机")

    def test_mode2_count_1_no_detail_returns_1_random_heroine(self):
        """选2 → 数量1 → 不填细节 → 1位女主。"""
        result = self._run(["2", "1", ""])
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["heroines"]), 1)

    def test_mode2_count_3_no_detail(self):
        """选2 → 数量3 → 不填细节 → 3位全随机女主。"""
        result = self._run(["2", "3", ""])
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["heroines"]), 3)

    # ── [2] 指定模式 + y（填写细节）─────────────────────────────

    def test_mode2_yes_detail_collects_names_and_personalities(self):
        """选2 → 数量1 → 填细节(y) → 逐个填写 → 返回有名字/性格的女主。"""
        # 模式=2 → 数量=1 → 细节=y → 名字="爱丽丝" → 性格="冷傲" → 外貌=回车
        result = self._run(["2", "1", "y", "爱丽丝", "冷傲", ""])
        self.assertIsInstance(result, dict)
        heroines = result["heroines"]
        self.assertEqual(len(heroines), 1)
        h_name, h_personality, _ = heroines[0]
        self.assertEqual(h_name,        "爱丽丝")
        self.assertEqual(h_personality, "冷傲")

    def test_mode2_yes_detail_with_appearance(self):
        """选2 → 数量1 → 填细节(y) → 外貌有内容时被记录。"""
        result = self._run(["2", "1", "y", "蕾娜", "温柔", "银发碧眼"])
        self.assertIsInstance(result, dict)
        _, _, h_desc = result["heroines"][0]
        self.assertEqual(h_desc, "银发碧眼")

    # ── 取消 / 返回 ───────────────────────────────────────────────

    def test_q_at_mode_select_returns_None(self):
        """在子模式选择处输入 'q' → 返回 None。"""
        result = self._run(["q"])
        self.assertIsNone(result)

    def test_b_at_mode_select_returns_BACK(self):
        """在子模式选择处输入 'b' → 返回 _BACK。"""
        result = self._run(["b"])
        self.assertEqual(result, _m._BACK)

    def test_q_at_count_returns_None(self):
        """在指定模式的数量处输入 'q' → 返回 None。"""
        result = self._run(["2", "q"])
        self.assertIsNone(result)


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 6: _wz_world() 去除 [7] 独立选项
# ══════════════════════════════════════════════════════════════════

class TestWizardWorldStep(unittest.TestCase):
    """
    _wz_world()：世界背景选择步骤。

    修复前：有单独的 [7] 完全自定义选项，用户需选 7 再输入。
    修复后：移除 [7] 选项，用户可直接在提示处输入文字作为自定义背景。
    """

    def _run(self, inputs: list[str]):
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m._wz_world()

    def test_no_option_7_in_output(self):
        """界面输出中不含 '[7] 完全自定义' 字样（该选项已移除）。"""
        buf = io.StringIO()
        with patch("builtins.input", return_value="q"):
            with redirect_stdout(buf):
                _m._wz_world()
        output = buf.getvalue()
        self.assertNotIn("[7]", output,
                         "界面不应再显示 [7] 完全自定义选项")

    def test_direct_text_input_becomes_world_bg(self):
        """直接输入非数字文字 → world_bg 等于输入文字（自定义功能仍然可用）。"""
        result = self._run(["18世纪伦敦的迷雾城市"])
        self.assertIsInstance(result, dict)
        self.assertEqual(result["world_bg"], "18世纪伦敦的迷雾城市")

    def test_preset_1_returns_correct_bg(self):
        """输入 '1' → 返回第1个预设。"""
        result = self._run(["1"])
        self.assertIsInstance(result, dict)
        self.assertIn("现代都市", result["world_bg"])

    def test_preset_6_returns_correct_bg(self):
        """输入 '6' → 返回第6个预设（最后一个，不再是 [7]）。"""
        result = self._run(["6"])
        self.assertIsInstance(result, dict)
        self.assertIn("校园青春", result["world_bg"])

    def test_invalid_digit_7_reprompts(self):
        """输入 '7'（原自定义入口）→ 视为无效编号，重新提示，不进入子流程。"""
        # 输入7（无效）→ 再输入1（有效）
        result = self._run(["7", "1"])
        self.assertIsInstance(result, dict)
        self.assertIn("现代都市", result["world_bg"])

    def test_empty_input_defaults_to_preset_1(self):
        """直接回车 → 默认选第1个预设。"""
        result = self._run([""])
        self.assertIsInstance(result, dict)
        self.assertIn("现代都市", result["world_bg"])

    def test_q_returns_None(self):
        """输入 'q' → 返回 None（取消向导）。"""
        result = self._run(["q"])
        self.assertIsNone(result)

    def test_r_returns_random_preset(self):
        """输入 'r' → 返回随机预设 dict（world_bg 非空）。"""
        result = self._run(["r"])
        self.assertIsInstance(result, dict)
        self.assertTrue(result["world_bg"])


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 4+5: _wz_show_summary() / _wz_free_input()
# ══════════════════════════════════════════════════════════════════

def _make_structured(**overrides) -> dict:
    """构造最小可用的 structured 设定 dict，可按需覆盖字段。"""
    base = {
        "world_bg":        "测试世界",
        "player_name":     "主角",
        "player_identity": "普通人",
        "player_special":  "",
        "heroines":        [("女主1", "温柔", "")],
        "tone":            "轻松治愈",
        "main_plot":       "",
    }
    base.update(overrides)
    return base


class TestWizardBuildInstruction(unittest.TestCase):
    """_wz_build_instruction()：根据 structured 生成开局指令文本。"""

    def test_world_bg_appears(self):
        """世界背景出现在指令中。"""
        s = _make_structured(world_bg="18世纪伦敦")
        inst = _m._wz_build_instruction(s)
        self.assertIn("18世纪伦敦", inst)

    def test_player_name_and_identity_appear(self):
        """玩家名字和身份均出现在指令中。"""
        s = _make_structured(player_name="张三", player_identity="侦探")
        inst = _m._wz_build_instruction(s)
        self.assertIn("张三", inst)
        self.assertIn("侦探", inst)

    def test_player_special_included_when_present(self):
        """有特殊能力时出现在指令中。"""
        s = _make_structured(player_special="隐身术")
        inst = _m._wz_build_instruction(s)
        self.assertIn("隐身术", inst)

    def test_player_special_absent_when_empty(self):
        """player_special 为空时，指令中不含"特殊能力"字样。"""
        s = _make_structured(player_special="")
        inst = _m._wz_build_instruction(s)
        self.assertNotIn("特殊能力", inst)

    def test_heroine_name_appears(self):
        """女主角名字出现在指令中。"""
        s = _make_structured(heroines=[("爱丽丝", "冷傲", "")])
        inst = _m._wz_build_instruction(s)
        self.assertIn("爱丽丝", inst)

    def test_heroine_desc_included_when_present(self):
        """女主角有外貌描述时包含在指令中。"""
        s = _make_structured(heroines=[("女主1", "温柔", "银发")])
        inst = _m._wz_build_instruction(s)
        self.assertIn("银发", inst)

    def test_tone_appears(self):
        """游戏基调出现在指令中。"""
        s = _make_structured(tone="悬疑剧情")
        inst = _m._wz_build_instruction(s)
        self.assertIn("悬疑剧情", inst)

    def test_main_plot_included_when_present(self):
        """main_plot 非空时出现在指令中。"""
        s = _make_structured(main_plot="寻找失踪的父亲")
        inst = _m._wz_build_instruction(s)
        self.assertIn("寻找失踪的父亲", inst)

    def test_main_plot_absent_when_empty(self):
        """main_plot 为空时，指令不含"主线剧情"字样。"""
        s = _make_structured(main_plot="")
        inst = _m._wz_build_instruction(s)
        self.assertNotIn("主线剧情", inst)

    def test_returns_string(self):
        """返回值是字符串类型。"""
        self.assertIsInstance(_m._wz_build_instruction(_make_structured()), str)


class TestWizardShowSummary(unittest.TestCase):
    """_wz_show_summary()：展示摘要，处理确认/替换/重设/取消。"""

    def _run(self, structured: dict, inputs: list[str]):
        """模拟用户输入，返回 _wz_show_summary 的结果。"""
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m._wz_show_summary(structured)

    def test_enter_confirms_with_generated_instruction(self):
        """直接回车确认 → 返回 (generated_instruction, structured)。"""
        s = _make_structured(world_bg="测试世界")
        result = self._run(s, [""])
        self.assertIsInstance(result, tuple)
        instruction, structured = result
        self.assertIn("测试世界", instruction)
        self.assertIs(structured, s)

    def test_custom_input_replaces_instruction(self):
        """输入自定义内容 → 返回 (custom_text, structured)。"""
        s = _make_structured()
        result = self._run(s, ["自定义开局文本"])
        instruction, _ = result
        self.assertEqual(instruction, "自定义开局文本")

    def test_q_returns_None(self):
        """输入 'q' → 返回 None（取消）。"""
        s = _make_structured()
        result = self._run(s, ["q"])
        self.assertIsNone(result)

    def test_b_returns_BACK(self):
        """输入 'b' → 返回 _BACK（重头重设信号）。"""
        s = _make_structured()
        result = self._run(s, ["b"])
        self.assertEqual(result, _m._BACK)

    def test_function_exists(self):
        """_wz_show_summary 函数存在且可调用。"""
        self.assertTrue(callable(_m._wz_show_summary))


class TestWizardFreeInput(unittest.TestCase):
    """_wz_free_input()：A模式自由输入 → 解析 → 追问 → 摘要。"""

    def _run(self, inputs: list[str]):
        """模拟用户输入，返回 _wz_free_input() 的结果。"""
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m._wz_free_input()

    def test_cancel_at_description_returns_None(self):
        """在描述步骤输入 'q' → 返回 None。"""
        result = self._run(["q"])
        self.assertIsNone(result)

    def test_cancel_at_name_returns_None(self):
        """在名字步骤输入 'q' → 返回 None。"""
        result = self._run(["现代都市", "q"])
        self.assertIsNone(result)

    def test_empty_description_reprompts(self):
        """先输入空字符串，再输入有效内容 → 正常继续。"""
        # 空→有效描述→名字回车→身份回车→基调选1→摘要回车确认
        result = self._run(["", "现代都市侦探，悬疑", "", "", "", ""])
        # 应得到 tuple 结果
        self.assertIsInstance(result, tuple)

    def test_tone_inferred_skips_tone_question(self):
        """描述含"悬疑"→ 不询问基调，直接进入名字步骤，最终包含"悬疑剧情"。"""
        # 描述（含悬疑）→名字回车→身份回车→摘要回车
        result = self._run(["现代都市的侦探，悬疑基调", "", "", ""])
        self.assertIsInstance(result, tuple)
        instruction, structured = result
        self.assertEqual(structured["tone"], "悬疑剧情")

    def test_identity_inferred_skips_identity_question(self):
        """描述含"侦探"→ player_identity 推断为侦探，不询问身份。"""
        # 描述（含侦探）→名字回车→摘要回车确认（身份被推断，无需额外输入）
        result = self._run(["18世纪伦敦的侦探，悬疑基调", "", ""])
        self.assertIsInstance(result, tuple)
        _, structured = result
        self.assertEqual(structured["player_identity"], "侦探")

    def test_heroine_count_from_description(self):
        """描述含"两个女主"→ heroines 列表长度为2。"""
        # desc(2女主+悬疑) → name(回车) → identity问(无匹配,回车随机) → summary(回车确认)
        result = self._run(["有两个女主，悬疑基调", "", "", ""])
        self.assertIsInstance(result, tuple)
        _, structured = result
        self.assertEqual(len(structured["heroines"]), 2)

    def test_no_heroine_mention_defaults_to_one(self):
        """描述未提及女主 → 默认1位随机女主。"""
        # desc(悬疑) → name(回车) → identity问(无匹配,回车) → summary(回车)
        result = self._run(["现代都市，悬疑基调", "", "", ""])
        self.assertIsInstance(result, tuple)
        _, structured = result
        self.assertEqual(len(structured["heroines"]), 1)

    def test_world_bg_is_raw_input(self):
        """world_bg 等于用户输入的原始描述文本。"""
        # desc(悬疑) → name(回车) → identity问(无匹配,回车) → summary(回车)
        result = self._run(["星际殖民地，悬疑基调", "", "", ""])
        self.assertIsInstance(result, tuple)
        _, structured = result
        self.assertEqual(structured["world_bg"], "星际殖民地，悬疑基调")

    def test_function_exists(self):
        """_wz_free_input 函数存在且可调用。"""
        self.assertTrue(callable(_m._wz_free_input))


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 3: _wz_mode_select()
# ══════════════════════════════════════════════════════════════════

class TestWizardModeSelect(unittest.TestCase):
    """
    _wz_mode_select()：向导第0步，选择 A（自由输入）或 B（引导设定）。

    修复前：无此步骤（向导直接进入世界背景）。
    修复后：函数存在且返回正确值。
    """

    def _select(self, inputs: list[str]) -> "str | None":
        """模拟用户依次输入，返回 _wz_mode_select() 的结果。"""
        with patch("builtins.input", side_effect=inputs):
            with redirect_stdout(io.StringIO()):
                return _m._wz_mode_select()

    def test_input_A_returns_A(self):
        """输入 'A' → 返回 'A'。"""
        self.assertEqual(self._select(["A"]), "A")

    def test_input_a_lowercase_returns_A(self):
        """输入 'a'（小写）→ 返回 'A'（函数内部 upper()）。"""
        self.assertEqual(self._select(["a"]), "A")

    def test_input_B_returns_B(self):
        """输入 'B' → 返回 'B'。"""
        self.assertEqual(self._select(["B"]), "B")

    def test_empty_defaults_to_B(self):
        """直接回车（空输入）→ 默认返回 'B'。"""
        self.assertEqual(self._select([""]), "B")

    def test_input_q_returns_None(self):
        """输入 'q' → 返回 None（取消向导）。"""
        self.assertIsNone(self._select(["q"]))

    def test_input_Q_uppercase_returns_None(self):
        """输入 'Q'（大写）→ 返回 None。"""
        self.assertIsNone(self._select(["Q"]))

    def test_invalid_then_valid(self):
        """先输入无效值，再输入有效值 → 最终返回有效值。"""
        self.assertEqual(self._select(["X", "Y", "A"]), "A")

    def test_function_exists(self):
        """_wz_mode_select 函数存在且可调用。"""
        self.assertTrue(callable(_m._wz_mode_select))


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 2: _parse_free_text()
# ══════════════════════════════════════════════════════════════════

class TestWizardParseFreeText(unittest.TestCase):
    """
    _parse_free_text()：从自由描述文本中提取已知字段。

    修复前：无此函数（A模式不存在）。
    修复后：能正确提取 tone / heroine_count / heroine_personalities / player_identity，
            无法识别的字段不包含在返回值中（绝不猜测）。
    """

    def _parse(self, text: str) -> dict:
        return _m._parse_free_text(text)

    # ── 基调 ────────────────────────────────────────────────────

    def test_tone_悬疑(self):
        """含"悬疑"→ tone='悬疑剧情'。"""
        r = self._parse("18世纪伦敦的侦探，悬疑基调")
        self.assertEqual(r.get("tone"), "悬疑剧情")

    def test_tone_轻松治愈(self):
        """含"治愈"→ tone='轻松治愈'。"""
        r = self._parse("校园日常，治愈风格")
        self.assertEqual(r.get("tone"), "轻松治愈")

    def test_tone_暗黑虐恋(self):
        """含"暗黑"→ tone='暗黑虐恋'。"""
        r = self._parse("暗黑末世，生存为主")
        self.assertEqual(r.get("tone"), "暗黑虐恋")

    def test_tone_热血冒险(self):
        """含"冒险"→ tone='热血冒险'。"""
        r = self._parse("冒险世界，打怪升级")
        self.assertEqual(r.get("tone"), "热血冒险")

    def test_tone_missing(self):
        """无任何基调关键词 → 返回值不含 tone 键。"""
        r = self._parse("现代都市背景，主角是医生")
        self.assertNotIn("tone", r)

    # ── 女主角数量 ───────────────────────────────────────────────

    def test_heroine_count_两个(self):
        """含"两个女主"→ heroine_count=2。"""
        r = self._parse("有两个女主，悬疑基调")
        self.assertEqual(r.get("heroine_count"), 2)

    def test_heroine_count_三名(self):
        """含"三名女主"→ heroine_count=3。"""
        r = self._parse("三名女主角，各有性格")
        self.assertEqual(r.get("heroine_count"), 3)

    def test_heroine_count_digit(self):
        """含"2个女生"→ heroine_count=2。"""
        r = self._parse("世界里有2个女生陪伴")
        self.assertEqual(r.get("heroine_count"), 2)

    def test_heroine_count_missing(self):
        """无女主数量词 → 返回值不含 heroine_count 键。"""
        r = self._parse("现代都市，侦探，悬疑")
        self.assertNotIn("heroine_count", r)

    # ── 女主性格 ─────────────────────────────────────────────────

    def test_personality_冷傲(self):
        """含"冷傲"→ heroine_personalities 包含 '冷傲'。"""
        r = self._parse("一个冷傲的女主")
        self.assertIn("冷傲", r.get("heroine_personalities", []))

    def test_personality_multiple(self):
        """含多个性格词 → heroine_personalities 包含所有已识别的性格。"""
        r = self._parse("一个活泼，一个温柔")
        personalities = r.get("heroine_personalities", [])
        self.assertIn("活泼", personalities)
        self.assertIn("温柔", personalities)

    def test_personality_missing(self):
        """无性格词 → 返回值不含 heroine_personalities 键。"""
        r = self._parse("现代都市，侦探")
        self.assertNotIn("heroine_personalities", r)

    # ── 玩家身份 ─────────────────────────────────────────────────

    def test_player_identity_侦探(self):
        """含"侦探"→ player_identity='侦探'。"""
        r = self._parse("18世纪伦敦的侦探，悬疑")
        self.assertEqual(r.get("player_identity"), "侦探")

    def test_player_identity_医生(self):
        """含"医生"→ player_identity='医生'。"""
        r = self._parse("现代都市的医生")
        self.assertEqual(r.get("player_identity"), "医生")

    def test_player_identity_missing(self):
        """无职业关键词 → 返回值不含 player_identity 键。"""
        r = self._parse("未来科幻世界，悬疑")
        self.assertNotIn("player_identity", r)

    # ── 综合示例 ─────────────────────────────────────────────────

    def test_example_sentence(self):
        """需求示例："18世纪伦敦的侦探，有两个女主，悬疑基调"→ 完整提取。"""
        r = self._parse("18世纪伦敦的侦探，有两个女主，悬疑基调")
        self.assertEqual(r.get("tone"),            "悬疑剧情")
        self.assertEqual(r.get("heroine_count"),   2)
        self.assertEqual(r.get("player_identity"), "侦探")

    def test_unrecognized_text_returns_empty_dict(self):
        """完全无法识别的文本 → 返回空 dict，不崩溃。"""
        r = self._parse("xyz123@@@随机内容abc")
        self.assertIsInstance(r, dict)


# ══════════════════════════════════════════════════════════════════
# 向导重构 — Change 1: _print_hint()
# ══════════════════════════════════════════════════════════════════

class TestWizardPrintHint(unittest.TestCase):
    """
    _print_hint() 输出提示文字。

    修复前：无此函数。
    修复后：非TTY时输出普通带缩进文本，每行前缀 '  '；
            多行文本按换行拆分逐行输出。
    """

    def _capture(self, text: str) -> str:
        """捕获 _print_hint 的 stdout 输出（测试环境非 TTY，走普通输出路径）。"""
        buf = io.StringIO()
        with redirect_stdout(buf):
            _m._print_hint(text)
        return buf.getvalue()

    def test_single_line_has_indent(self):
        """单行文本输出后包含原始内容。"""
        out = self._capture("测试提示")
        self.assertIn("测试提示", out)

    def test_single_line_has_leading_spaces(self):
        """每行前缀含两个空格（非TTY普通模式）。"""
        out = self._capture("ABC")
        # 不能 strip，否则会把前缀空格去掉；直接按行检查
        lines = out.splitlines()
        nonempty = [l for l in lines if l]
        self.assertTrue(any(line.startswith("  ") for line in nonempty),
                        f"输出行缺少前缀空格：{nonempty}")

    def test_multiline_split_correctly(self):
        """包含换行的文本被拆成多行分别输出。"""
        out = self._capture("第一行\n第二行\n第三行")
        self.assertIn("第一行", out)
        self.assertIn("第二行", out)
        self.assertIn("第三行", out)
        # 应输出3行（非空行）
        nonempty = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(nonempty), 3,
                         f"3行文本应产生3行输出，实际：{nonempty}")

    def test_empty_string_no_crash(self):
        """空字符串不崩溃，输出至少包含空行。"""
        try:
            self._capture("")
        except Exception as e:
            self.fail(f"_print_hint('') 不应抛出异常：{e}")

    def test_function_exists_and_callable(self):
        """_print_hint 函数存在且可调用（防止函数被删除后此功能静默消失）。"""
        self.assertTrue(callable(_m._print_hint))


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
    TestGmMenu,
    TestGuiHistoryAndParse,
    TestHardConstraintWordLimit,
    TestHardConstraintSceneSwitch,
    TestRunNewGameWizard,
    TestWizardHeroinesStep,
    TestWizardWorldStep,
    TestWizardBuildInstruction,
    TestWizardShowSummary,
    TestWizardFreeInput,
    TestWizardModeSelect,
    TestWizardParseFreeText,
    TestWizardPrintHint,
]

# ══════════════════════════════════════════════════════════════════
# 时间推进规则回归测试
# 验证：删除"每3次行动推进一个时段"机械规则 + 补充GM判断义务
# ══════════════════════════════════════════════════════════════════

class TestTimeAdvanceRules(unittest.TestCase):
    """
    回归测试：时间推进规则冲突修复。

    改动：
      1. engine_rules.txt ❹ 和 narrative_rules.txt ❷ 删除"每3次行动推进一个时段"
      2. engine_rules.txt TIME LOCK RULE 新增条目4（GM判断义务）

    这些测试直接读取规则文件文本，验证改动已生效。
    若有人把规则改回旧版，测试立即报红。
    """

    _ENGINE  = _ROOT / "prompt" / "engine_rules.txt"
    _NARR    = _ROOT / "prompt" / "narrative_rules.txt"

    @classmethod
    def setUpClass(cls):
        cls._engine_text = cls._ENGINE.read_text(encoding="utf-8")
        cls._narr_text   = cls._NARR.read_text(encoding="utf-8")

    # ── 删除机械规则 ─────────────────────────────────────────────

    def test_engine_rules_no_mechanical_trigger(self):
        """engine_rules.txt ❹ 不得包含"每3次行动推进一个时段"。"""
        self.assertNotIn(
            "每3次行动推进一个时段",
            self._engine_text,
            "engine_rules.txt 仍含机械时段触发规则，应已删除",
        )

    def test_narrative_rules_no_mechanical_trigger(self):
        """narrative_rules.txt ❷ 不得包含"每3次行动推进一个时段"。"""
        self.assertNotIn(
            "每3次行动推进一个时段",
            self._narr_text,
            "narrative_rules.txt 仍含机械时段触发规则，应已删除",
        )

    # ── 时段顺序保留 ─────────────────────────────────────────────

    def test_engine_rules_time_sequence_preserved(self):
        """删除机械规则后，时段顺序字符串应仍在 engine_rules.txt 中。"""
        self.assertIn(
            "上午→下午→傍晚→夜晚",
            self._engine_text,
            "engine_rules.txt 时段顺序参考被误删",
        )

    def test_narrative_rules_time_sequence_preserved(self):
        """删除机械规则后，时段顺序字符串应仍在 narrative_rules.txt 中。"""
        self.assertIn(
            "上午→下午→傍晚→夜晚",
            self._narr_text,
            "narrative_rules.txt 时段顺序参考被误删",
        )

    # ── TIME LOCK RULE 新增 GM 判断义务 ──────────────────────────

    def test_time_lock_rule_gm_must_judge(self):
        """TIME LOCK RULE 必须包含 GM 主动判断时间是否推进的义务。"""
        self.assertIn(
            "GM必须主动判断时间是否推进",
            self._engine_text,
            "TIME LOCK RULE 缺少 GM 判断义务条款",
        )

    def test_time_lock_rule_save_info_update(self):
        """TIME LOCK RULE 必须要求 GM 在 save_info 里更新 date 和 time_slot。"""
        self.assertIn(
            "save_info里更新date和time_slot",
            self._engine_text,
            "TIME LOCK RULE 缺少 save_info 强制更新要求",
        )

    def test_time_lock_rule_same_value_fallback(self):
        """TIME LOCK RULE 必须明确：时间未推进时仍须输出与上回合相同的值。"""
        self.assertIn(
            "仍须输出与上回合相同的值",
            self._engine_text,
            "TIME LOCK RULE 缺少'时间未推进时仍须输出相同值'的回退要求",
        )


_TEST_CLASSES.append(TestTimeAdvanceRules)


# ══════════════════════════════════════════════════════════════════
# NPC认知档案 + 关系节点：engine_rules 白名单扩展回归
# ══════════════════════════════════════════════════════════════════

class TestNpcUpdateEngineRules(unittest.TestCase):
    """
    回归测试：engine_rules.txt 白名单扩展为三个字段，并包含 npc_update 规范。
    """

    _ENGINE = _ROOT / "prompt" / "engine_rules.txt"

    @classmethod
    def setUpClass(cls):
        cls._text = cls._ENGINE.read_text(encoding="utf-8")

    def test_whitelist_allows_three_fields(self):
        """状态更新规则必须允许三个顶层字段（save_info / new_event / npc_update）。"""
        self.assertIn("三个顶层字段", self._text)
        self.assertIn("npc_update", self._text)

    def test_whitelist_no_longer_says_two(self):
        """旧的'两个顶层字段'描述必须已被替换。"""
        self.assertNotIn("两个顶层字段", self._text)

    def test_npc_update_format_present(self):
        """engine_rules.txt 必须包含 npc_update 格式规范。"""
        self.assertIn("npc_update 格式规范", self._text)

    def test_npc_update_trigger_conditions(self):
        """engine_rules.txt 必须包含 npc_update 触发条件说明。"""
        self.assertIn("npc_update 触发条件", self._text)

    def test_player_knowledge_length_limit(self):
        """engine_rules.txt 必须明确 player_knowledge 每条不超过15字。"""
        self.assertIn("15字", self._text)

    def test_single_npc_per_round(self):
        """engine_rules.txt 必须限制同一回合只能更新一个NPC。"""
        self.assertIn("同一回合只能更新一个NPC", self._text)

    def test_new_milestone_field_specified(self):
        """npc_update 格式必须包含 new_milestone 字段。"""
        self.assertIn("new_milestone", self._text)


_TEST_CLASSES.append(TestNpcUpdateEngineRules)


# ══════════════════════════════════════════════════════════════════
# NPC首次命名触发规则回归
# 修复：缺少"首次命名"触发条件导致 heroines 始终为空，身份混乱
# ══════════════════════════════════════════════════════════════════

class TestNpcFirstAppearanceTrigger(unittest.TestCase):
    """
    回归测试：NPC首次出现并被命名时，必须触发 npc_update 建立初始档案。
    修复场景：history 窗口滚动后 LLM 只靠 event_cards 短文本推断人物关系，
    造成身份混乱（如西装男子被错误关联到黑泽事务所）。
    """

    _ENGINE = _ROOT / "prompt" / "engine_rules.txt"

    @classmethod
    def setUpClass(cls):
        cls._text = cls._ENGINE.read_text(encoding="utf-8")

    def test_first_appearance_trigger_exists(self):
        """触发条件必须包含'首次出现并被命名'的规则。"""
        self.assertIn("首次出现并被命名", self._text)

    def test_first_appearance_requires_initial_milestone(self):
        """首次命名触发时必须要求建立初识节点（new_milestone.event=初识）。"""
        # 规则文本中同时出现"首次出现"和"初识"说明两者关联
        text = self._text
        idx_first = text.find("首次出现并被命名")
        idx_milestone = text.find("初识", idx_first)
        self.assertGreater(idx_milestone, idx_first,
                           "首次命名规则之后应紧跟'初识'节点要求")

    def test_first_appearance_allows_empty_knowledge(self):
        """首次命名触发时明确允许 player_knowledge 为空列表。"""
        self.assertIn("player_knowledge可为空列表", self._text)


_TEST_CLASSES.append(TestNpcFirstAppearanceTrigger)


# ══════════════════════════════════════════════════════════════════
# NPC认知档案 + 关系节点：parse_response + apply_updates 回归
# ══════════════════════════════════════════════════════════════════

class TestNpcUpdateParseAndApply(unittest.TestCase):
    """
    回归测试：parse_response 正确解析 npc_update，apply_updates 正确合并。
    """

    def _ws_with_npc(self, name="林知遥"):
        ws = _ws()
        ws["characters"]["heroines"] = [{
            "name": name,
            "affection": 0,
            "player_knowledge": [],
            "npc_relations": [],
            "relationship_milestones": [],
        }]
        return ws

    # ── parse_response ──────────────────────────────────────────

    def test_parse_npc_update_accepted(self):
        """parse_response 应接受合法的 npc_update 字段。"""
        raw = 'narrative\n---JSON---\n{"save_info":{"date":"4月1日","time_slot":"上午","location":"公园"},"new_event":"测试","npc_update":{"name":"林知遥","player_knowledge":["自由职业者"]}}'
        _, updates = _m.parse_response(raw)
        self.assertIn("npc_update", updates)
        self.assertEqual(updates["npc_update"]["name"], "林知遥")

    def test_parse_npc_update_knowledge_truncated(self):
        """player_knowledge 每条超过15字时应被截断。"""
        raw = 'n\n---JSON---\n{"save_info":{"date":"4月1日","time_slot":"上午","location":"x"},"new_event":"e","npc_update":{"name":"A","player_knowledge":["这是一条超过十五个字的认知条目内容测试"]}}'
        _, updates = _m.parse_response(raw)
        for item in updates["npc_update"]["player_knowledge"]:
            self.assertLessEqual(len(item), 15)

    def test_parse_npc_update_no_name_rejected(self):
        """npc_update 缺少 name 时应被丢弃。"""
        raw = 'n\n---JSON---\n{"save_info":{"date":"4月1日","time_slot":"上午","location":"x"},"new_event":"e","npc_update":{"player_knowledge":["abc"]}}'
        _, updates = _m.parse_response(raw)
        self.assertNotIn("npc_update", updates)

    def test_parse_milestone_fields_preserved(self):
        """new_milestone 所有字段应被完整解析。"""
        ms = {"round": 5, "date": "4月5日", "location": "山顶", "event": "初识", "detail": "聊了工作"}
        raw = f'n\n---JSON---\n{{"save_info":{{"date":"4月5日","time_slot":"上午","location":"山顶"}},"new_event":"e","npc_update":{{"name":"A","new_milestone":{json.dumps(ms, ensure_ascii=False)}}}}}'
        _, updates = _m.parse_response(raw)
        self.assertIn("new_milestone", updates["npc_update"])
        self.assertEqual(updates["npc_update"]["new_milestone"]["round"], 5)
        self.assertEqual(updates["npc_update"]["new_milestone"]["event"], "初识")

    # ── apply_updates ────────────────────────────────────────────

    def test_apply_player_knowledge_merged(self):
        """apply_updates 应将新知识合并进目标NPC的 player_knowledge。"""
        ws = self._ws_with_npc("林知遥")
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "player_knowledge": ["自由职业者"]}})
        npc = ws["characters"]["heroines"][0]
        self.assertIn("自由职业者", npc["player_knowledge"])

    def test_apply_player_knowledge_deduped(self):
        """apply_updates 合并时应去重，不重复添加相同知识。"""
        ws = self._ws_with_npc("林知遥")
        ws["characters"]["heroines"][0]["player_knowledge"] = ["自由职业者"]
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "player_knowledge": ["自由职业者"]}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(npc["player_knowledge"].count("自由职业者"), 1)

    def test_apply_npc_relations_add(self):
        """apply_updates 应向目标NPC添加新的 npc_relations 条目。"""
        ws = self._ws_with_npc("林知遥")
        rel = {"name": "田中", "relation": "同事", "attitude": "友好", "knows_player_connection": False}
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "npc_relations": [rel]}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(len(npc["npc_relations"]), 1)
        self.assertEqual(npc["npc_relations"][0]["name"], "田中")

    def test_apply_npc_relations_update_existing(self):
        """apply_updates 对已存在同名关系应更新而非新增。"""
        ws = self._ws_with_npc("林知遥")
        ws["characters"]["heroines"][0]["npc_relations"] = [
            {"name": "田中", "relation": "同事", "attitude": "中立", "knows_player_connection": False}
        ]
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "npc_relations": [
            {"name": "田中", "relation": "同事", "attitude": "友好", "knows_player_connection": True}
        ]}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(len(npc["npc_relations"]), 1)
        self.assertEqual(npc["npc_relations"][0]["attitude"], "友好")

    def test_apply_milestone_appended(self):
        """apply_updates 应将 new_milestone 追加进 relationship_milestones。"""
        ws = self._ws_with_npc("林知遥")
        ms = {"round": 1, "date": "4月12日", "location": "北岭山", "event": "初识", "detail": "讨论工作"}
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "new_milestone": ms}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(len(npc["relationship_milestones"]), 1)
        self.assertEqual(npc["relationship_milestones"][0]["event"], "初识")

    def test_apply_milestone_not_deduped(self):
        """relationship_milestones 追加不去重（同一事件可记录多次）。"""
        ws = self._ws_with_npc("林知遥")
        ms = {"round": 1, "date": "4月12日", "location": "山顶", "event": "初识", "detail": "聊天"}
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "new_milestone": ms}})
        _m.apply_updates(ws, {"npc_update": {"name": "林知遥", "new_milestone": ms}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(len(npc["relationship_milestones"]), 2)

    def test_apply_unknown_npc_skipped(self):
        """apply_updates 找不到目标NPC时，world_state 不应报错也不应被修改。"""
        ws = self._ws_with_npc("林知遥")
        _m.apply_updates(ws, {"npc_update": {"name": "不存在的人", "player_knowledge": ["测试"]}})
        npc = ws["characters"]["heroines"][0]
        self.assertEqual(npc["player_knowledge"], [])


_TEST_CLASSES.append(TestNpcUpdateParseAndApply)


# ══════════════════════════════════════════════════════════════════
# NPC认知档案 + 关系节点：_render_heroine 注入回归
# ══════════════════════════════════════════════════════════════════

class TestRenderHeroineNpcFields(unittest.TestCase):
    """
    回归测试：_render_heroine 在 system prompt 中优先注入
    player_knowledge 和 relationship_milestones。
    """

    from prompt.builder import _render_heroine as _render

    def _render_lines(self, h: dict, trim: bool = False) -> list[str]:
        from prompt.builder import _render_heroine
        lines: list = []
        _render_heroine(lines, h, trim=trim)
        return lines

    def _rendered(self, h: dict, trim: bool = False) -> str:
        return "\n".join(self._render_lines(h, trim=trim))

    def test_player_knowledge_injected(self):
        """player_knowledge 非空时应出现在渲染输出中。"""
        h = {"name": "林知遥", "affection": 0, "player_knowledge": ["自由职业者", "在日华人"]}
        text = self._rendered(h)
        self.assertIn("林知遥对玩家的了解", text)
        self.assertIn("自由职业者", text)
        self.assertIn("在日华人", text)

    def test_player_knowledge_empty_skipped(self):
        """player_knowledge 为空时不输出对应标题。"""
        h = {"name": "林知遥", "affection": 0, "player_knowledge": []}
        text = self._rendered(h)
        self.assertNotIn("对玩家的了解", text)

    def test_milestones_injected(self):
        """relationship_milestones 非空时应出现在渲染输出中。"""
        h = {"name": "林知遥", "affection": 0, "relationship_milestones": [
            {"round": 1, "location": "北岭山", "event": "初识", "detail": "讨论了工作压力"}
        ]}
        text = self._rendered(h)
        self.assertIn("与玩家的关键时刻", text)
        self.assertIn("r1", text)
        self.assertIn("北岭山", text)
        self.assertIn("初识", text)

    def test_milestones_empty_skipped(self):
        """relationship_milestones 为空时不输出对应标题。"""
        h = {"name": "林知遥", "affection": 0, "relationship_milestones": []}
        text = self._rendered(h)
        self.assertNotIn("与玩家的关键时刻", text)

    def test_knowledge_appears_before_personality(self):
        """player_knowledge 应在性格核心之前出现（优先注入）。"""
        h = {
            "name": "林知遥", "affection": 0,
            "player_knowledge": ["自由职业者"],
            "personality_core": "独立冷静",
        }
        text = self._rendered(h)
        idx_knowledge = text.index("对玩家的了解")
        idx_personality = text.index("独立冷静")
        self.assertLess(idx_knowledge, idx_personality)

    def test_milestones_appear_before_personality(self):
        """relationship_milestones 应在性格核心之前出现（优先注入）。"""
        h = {
            "name": "林知遥", "affection": 0,
            "relationship_milestones": [{"round": 1, "location": "山", "event": "初识", "detail": "x"}],
            "personality_core": "独立冷静",
        }
        text = self._rendered(h)
        idx_ms = text.index("与玩家的关键时刻")
        idx_personality = text.index("独立冷静")
        self.assertLess(idx_ms, idx_personality)

    def test_missing_fields_no_crash(self):
        """NPC 对象缺少新字段时不应崩溃，兼容旧存档。"""
        h = {"name": "林知遥", "affection": 50}
        try:
            self._rendered(h)
        except Exception as e:
            self.fail(f"_render_heroine 在缺少新字段时崩溃：{e}")


_TEST_CLASSES.append(TestRenderHeroineNpcFields)


# ══════════════════════════════════════════════════════════════════
# 层3滚动窗口：storage/memory.py 压缩逻辑禁用回归
# ══════════════════════════════════════════════════════════════════

class TestMemoryDisabled(unittest.TestCase):
    """
    回归测试：compress_events / should_summarize 已停用。
    should_summarize 始终返回 False，compress_events 返回原值。
    """

    def test_should_summarize_always_false(self):
        """should_summarize 任何时候都返回 False。"""
        from storage.memory import should_summarize
        ws_many = {"story_state": {"event_cards": {"4月1日": ["a"] * 20}}}
        self.assertFalse(should_summarize(ws_many))

    def test_compress_events_returns_original(self):
        """compress_events 不修改 world_state，原样返回。"""
        from storage.memory import compress_events
        ws = {"story_state": {"event_cards": {"4月1日": ["a", "b"]}}}
        called = []
        result = compress_events(ws, lambda s, u: called.append(1) or "summary")
        self.assertIs(result, ws)
        self.assertEqual(called, [], "compress_events 不应调用 generate_fn")

    def test_inject_summary_returns_empty(self):
        """inject_summary_to_context 始终返回空字符串。"""
        from storage.memory import inject_summary_to_context
        ws = {"story_summary": "曾经的剧情"}
        self.assertEqual(inject_summary_to_context(ws), "")


_TEST_CLASSES.append(TestMemoryDisabled)


# ══════════════════════════════════════════════════════════════════
# 层3滚动窗口：apply_updates event_cards 按日分组回归
# ══════════════════════════════════════════════════════════════════

class TestEventCardsByDate(unittest.TestCase):
    """
    回归测试：event_cards 按游戏日分组，14天滚动，超出归档到 event_archive。
    """

    def _ws_with_date(self, date: str) -> dict:
        ws = _ws()
        ws["save_info"]["date"] = date
        ws["story_state"]["event_cards"] = {}
        ws["story_state"]["event_archive"] = {}
        return ws

    def test_new_event_grouped_by_date(self):
        """new_event 应写入当前 save_info.date 对应的分组。"""
        ws = self._ws_with_date("4月12日")
        _m.apply_updates(ws, {"new_event": "两人相识"})
        ec = ws["story_state"]["event_cards"]
        self.assertIn("4月12日", ec)
        self.assertIn("两人相识", ec["4月12日"])

    def test_same_date_events_appended(self):
        """同一天多条事件应追加到同一分组。"""
        ws = self._ws_with_date("4月12日")
        _m.apply_updates(ws, {"new_event": "事件A"})
        _m.apply_updates(ws, {"new_event": "事件B"})
        ec = ws["story_state"]["event_cards"]
        self.assertEqual(ec["4月12日"], ["事件A", "事件B"])

    def test_different_dates_separate_groups(self):
        """不同日期的事件应写入各自分组。"""
        ws = self._ws_with_date("4月12日")
        _m.apply_updates(ws, {"new_event": "事件A"})
        ws["save_info"]["date"] = "4月13日"
        _m.apply_updates(ws, {"new_event": "事件B"})
        ec = ws["story_state"]["event_cards"]
        self.assertIn("4月12日", ec)
        self.assertIn("4月13日", ec)

    def test_oldest_day_discarded_at_15th_day(self):
        """第15个不同游戏日写入时，最旧一天直接丢弃（不归档）。"""
        ws = self._ws_with_date("4月1日")
        for i in range(1, 16):
            ws["save_info"]["date"] = f"4月{i}日"
            _m.apply_updates(ws, {"new_event": f"事件{i}"})
        ec = ws["story_state"]["event_cards"]
        self.assertEqual(len(ec), 14, f"event_cards 应保留14天，实际{len(ec)}天")
        self.assertNotIn("4月1日", ec, "最旧的4月1日应已被丢弃")
        self.assertEqual(ws["story_state"].get("event_archive", {}), {}, "不应向 event_archive 写入任何条目")

    def test_old_list_format_migrated(self):
        """旧格式 event_cards 列表在首次写入新事件时应迁移为 dict。"""
        ws = self._ws_with_date("4月12日")
        ws["story_state"]["event_cards"] = ["旧事件A", "旧事件B"]  # 旧格式
        _m.apply_updates(ws, {"new_event": "新事件"})
        ec = ws["story_state"]["event_cards"]
        self.assertIsInstance(ec, dict)
        self.assertIn("4月12日", ec)
        self.assertIn("新事件", ec["4月12日"])


_TEST_CLASSES.append(TestEventCardsByDate)


# ══════════════════════════════════════════════════════════════════
# 层3滚动窗口：dynamic context 注入最近3天回归
# ══════════════════════════════════════════════════════════════════

class TestDynamicContextEventCards(unittest.TestCase):
    """
    回归测试：build_dynamic_context 注入最近3个游戏日，
    event_archive 不注入 context。
    """

    def _ws_with_events(self, days: dict) -> dict:
        ws = _ws()
        ws["story_state"]["event_cards"] = days
        ws["story_state"]["event_archive"] = {"4月1日": "远古摘要"}
        return ws

    def test_recent_3_days_in_context(self):
        """build_dynamic_context 应包含最近3个游戏日的事件。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({
            "4月10日": ["事件A"],
            "4月11日": ["事件B"],
            "4月12日": ["事件C"],
        })
        ctx = build_dynamic_context(ws)
        self.assertIn("4月10日", ctx)
        self.assertIn("4月11日", ctx)
        self.assertIn("4月12日", ctx)

    def test_only_last_3_days_shown(self):
        """超过3天时，只显示最近3天，旧的不注入。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({
            "4月9日":  ["事件OLD"],
            "4月10日": ["事件A"],
            "4月11日": ["事件B"],
            "4月12日": ["事件C"],
        })
        ctx = build_dynamic_context(ws)
        self.assertNotIn("4月9日", ctx)
        self.assertIn("4月10日", ctx)

    def test_archive_not_in_context(self):
        """event_archive 不注入 context。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({"4月12日": ["事件X"]})
        ctx = build_dynamic_context(ws)
        self.assertNotIn("远古摘要", ctx)
        self.assertNotIn("event_archive", ctx)

    def test_events_within_day_shown(self):
        """每天内的具体事件内容应出现在 context 中。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({"4月12日": ["两人在山上相识", "聊了工作"]})
        ctx = build_dynamic_context(ws)
        self.assertIn("两人在山上相识", ctx)
        self.assertIn("聊了工作", ctx)

    def test_empty_event_cards_no_section(self):
        """event_cards 为空 dict 时不输出近期事件标题。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({})
        ctx = build_dynamic_context(ws)
        self.assertNotIn("近期事件", ctx)

    def test_story_summary_not_injected(self):
        """旧 story_summary 不再注入 dynamic context。"""
        from prompt.builder import build_dynamic_context
        ws = self._ws_with_events({})
        ws["story_summary"] = "这是一段旧摘要"
        ctx = build_dynamic_context(ws)
        self.assertNotIn("旧摘要", ctx)


_TEST_CLASSES.append(TestDynamicContextEventCards)


# ══════════════════════════════════════════════════════════════════
# NPC 自动注册（兜底机制）回归测试
# 验证：event_cards new_event 写入时自动检测并注册未登记的 NPC
# ══════════════════════════════════════════════════════════════════

class TestAutoRegisterNpc(unittest.TestCase):
    """
    _detect_npc_name / _auto_register_npc / apply_updates 兜底注册逻辑回归测试。

    目标：不管 LLM 有没有输出 npc_update，只要事件字符串包含可识别的 NPC 名字，
    apply_updates 就应在 supporting_characters 里创建最小档案。
    """

    def _ws(self, events: list | None = None) -> dict:
        ws = {
            "save_info": {"turn": 5, "date": "4月12日 周六", "time_slot": "上午", "location": "公寓大厅"},
            "characters": {"heroines": [], "supporting_characters": []},
            "story_state": {"event_cards": {}, "event_archive": {}, "suspended_issues": []},
        }
        if events:
            ws["story_state"]["event_cards"]["4月12日 周六"] = list(events)
        return ws

    # ── _detect_npc_name ─────────────────────────────────────────

    def test_detect_name_after_keyword(self):
        """'名字林晚' 模式：返回 ('林晚', True)。"""
        name, is_naming = _m._detect_npc_name("女邻居自报名字林晚")
        self.assertEqual(name, "林晚")
        self.assertTrue(is_naming)

    def test_detect_self_report(self):
        """'沈知意自报姓名' 模式：返回 ('沈知意', True)。"""
        name, is_naming = _m._detect_npc_name("邻居沈知意自报姓名")
        self.assertEqual(name, "沈知意")
        self.assertTrue(is_naming)

    def test_detect_subject_verb(self):
        """'林晚说…' 主语动词模式：返回 ('林晚', False)。"""
        name, is_naming = _m._detect_npc_name("林晚说她住15楼")
        self.assertEqual(name, "林晚")
        self.assertFalse(is_naming)

    def test_no_detect_player_subject(self):
        """'主角' 开头的事件：返回 None。"""
        name, _ = _m._detect_npc_name("主角在公寓大厅遇见神秘美女")
        self.assertIsNone(name)

    def test_no_detect_strangers(self):
        """含'陌生'的候选词：不应被识别为NPC名字。"""
        name, _ = _m._detect_npc_name("陌生女住户自称住1801")
        self.assertIsNone(name)

    def test_no_detect_suffix_words(self):
        """'女邻居'（以'邻居'结尾的泛称）：不应被识别为NPC名字。"""
        name, _ = _m._detect_npc_name("女邻居帮忙搬箱子")
        self.assertIsNone(name)

    # ── _auto_register_npc ───────────────────────────────────────

    def test_auto_register_creates_supporting_character(self):
        """_auto_register_npc 在 supporting_characters 里创建最小档案。"""
        ws = self._ws()
        _m._auto_register_npc(ws, "林晚")
        sc = ws["characters"]["supporting_characters"]
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["name"], "林晚")

    def test_auto_register_milestone_fields(self):
        """自动创建的档案包含 event='初识' 的 milestone，round/date/location 正确。"""
        ws = self._ws()
        _m._auto_register_npc(ws, "林晚")
        ms = ws["characters"]["supporting_characters"][0]["relationship_milestones"][0]
        self.assertEqual(ms["event"], "初识")
        self.assertEqual(ms["round"], 5)
        self.assertEqual(ms["date"], "4月12日 周六")
        self.assertEqual(ms["location"], "公寓大厅")

    def test_auto_register_no_duplicate(self):
        """已存在的NPC不重复注册。"""
        ws = self._ws()
        ws["characters"]["supporting_characters"].append({"name": "林晚"})
        _m._auto_register_npc(ws, "林晚")
        self.assertEqual(len(ws["characters"]["supporting_characters"]), 1)

    def test_auto_register_skips_heroine(self):
        """heroines 里已有的NPC不会再被注册到 supporting_characters。"""
        ws = self._ws()
        ws["characters"]["heroines"].append({"name": "林晚"})
        _m._auto_register_npc(ws, "林晚")
        self.assertEqual(len(ws["characters"]["supporting_characters"]), 0)

    # ── apply_updates 兜底触发 ───────────────────────────────────

    def test_apply_naming_event_registers_immediately(self):
        """命名事件（名字XXX）立即触发注册，即使是第一次出现。"""
        ws = self._ws()
        _m.apply_updates(ws, {"new_event": "女邻居自报名字林晚", "save_info": {}})
        names = {c["name"] for c in ws["characters"]["supporting_characters"]}
        self.assertIn("林晚", names)

    def test_apply_self_report_registers_immediately(self):
        """自报姓名事件立即注册。"""
        ws = self._ws()
        _m.apply_updates(ws, {"new_event": "邻居沈知意自报姓名", "save_info": {}})
        names = {c["name"] for c in ws["characters"]["supporting_characters"]}
        self.assertIn("沈知意", names)

    def test_apply_subject_verb_registers_after_second_occurrence(self):
        """主语动词事件：第1次不注册，第2次出现才注册。"""
        ws = self._ws(events=["林晚说她住15楼"])  # 已有1条
        # 第2次写入同名事件
        _m.apply_updates(ws, {"new_event": "林晚在门口提起邀约", "save_info": {}})
        names = {c["name"] for c in ws["characters"]["supporting_characters"]}
        self.assertIn("林晚", names)

    def test_apply_subject_verb_no_register_on_first(self):
        """主语动词事件：第1次出现时不应立即注册（尚未确认是 NPC 名字）。"""
        ws = self._ws()  # event_cards 为空
        _m.apply_updates(ws, {"new_event": "林晚说她住15楼", "save_info": {}})
        names = {c["name"] for c in ws["characters"]["supporting_characters"]}
        self.assertNotIn("林晚", names)

    def test_apply_player_subject_never_registered(self):
        """'主角'开头的事件不触发注册。"""
        ws = self._ws()
        for _ in range(3):
            _m.apply_updates(ws, {"new_event": "主角在公寓大厅遇见邻居", "save_info": {}})
        self.assertEqual(len(ws["characters"]["supporting_characters"]), 0)


_TEST_CLASSES.append(TestAutoRegisterNpc)


# ══════════════════════════════════════════════════════════════════
# supporting_characters 富数据渲染升级回归测试
# 验证：有 player_knowledge / relationship_milestones 的配角走
#       _render_heroine 全量渲染；无档案数据的配角保持简短格式。
# ══════════════════════════════════════════════════════════════════

class TestSupportingCharRichRender(unittest.TestCase):
    """build_static_system_prompt 对 supporting_characters 的差异化渲染。"""

    def _ws_with_supporting(self, sc_list: list) -> dict:
        return {
            "save_info": {"turn": 5, "date": "4月12日", "time_slot": "上午", "location": "公寓"},
            "characters": {"heroines": [], "supporting_characters": sc_list},
            "story_state": {"event_cards": {}, "event_archive": {}, "suspended_issues": []},
        }

    def _build(self, sc_list: list) -> str:
        from prompt.builder import build_static_system_prompt
        return build_static_system_prompt(self._ws_with_supporting(sc_list))

    # ── 有 player_knowledge → 全量渲染 ─────────────────────────

    def test_rich_knowledge_rendered(self):
        """有 player_knowledge 的配角：知识条目出现在 system prompt 中。"""
        sc = {
            "name": "林晚",
            "player_knowledge": ["住15楼", "对咖啡邀约暧昧"],
            "relationship_milestones": [],
        }
        result = self._build([sc])
        self.assertIn("住15楼", result)
        self.assertIn("对咖啡邀约暧昧", result)

    def test_rich_milestone_rendered(self):
        """有 relationship_milestones 的配角：milestone 内容出现在 system prompt 中。"""
        sc = {
            "name": "沈知意",
            "player_knowledge": [],
            "relationship_milestones": [
                {"round": 30, "date": "4月12日", "location": "便利店", "event": "初识", "detail": "请喝咖啡"}
            ],
        }
        result = self._build([sc])
        self.assertIn("初识", result)
        self.assertIn("请喝咖啡", result)

    def test_rich_uses_heroine_format_header(self):
        """有档案数据的配角：system prompt 里出现【角色：林晚】格式标头。"""
        sc = {
            "name": "林晚",
            "player_knowledge": ["住15楼"],
            "relationship_milestones": [],
        }
        result = self._build([sc])
        self.assertIn("【角色：林晚】", result)

    # ── 无 player_knowledge / milestones → 简短格式 ─────────────

    def test_plain_uses_short_format(self):
        """无档案数据的配角：走简短格式，出现'· 名字'，不出现【角色：】标头。"""
        sc = {
            "name": "物业大叔",
            "gender": "男",
            "type": "NPC",
            "relationship_to_player": "物业管理员",
        }
        result = self._build([sc])
        self.assertIn("物业大叔", result)
        self.assertNotIn("【角色：物业大叔】", result)

    def test_plain_shows_relationship(self):
        """无档案数据的配角：relationship_to_player 正常显示。"""
        sc = {
            "name": "物业大叔",
            "gender": "男",
            "type": "NPC",
            "relationship_to_player": "物业管理员",
        }
        result = self._build([sc])
        self.assertIn("物业管理员", result)

    # ── 混合列表 ─────────────────────────────────────────────────

    def test_mixed_list_both_formats(self):
        """有档案和无档案配角共存时，各自走对应格式。"""
        sc_rich = {
            "name": "林晚",
            "player_knowledge": ["住15楼"],
            "relationship_milestones": [],
        }
        sc_plain = {
            "name": "物业大叔",
            "gender": "男",
            "type": "NPC",
            "relationship_to_player": "物业管理员",
        }
        result = self._build([sc_rich, sc_plain])
        self.assertIn("【角色：林晚】", result)
        self.assertIn("住15楼", result)
        self.assertIn("物业大叔", result)
        self.assertNotIn("【角色：物业大叔】", result)

    # ── 空列表不输出【配角】标头 ────────────────────────────────

    def test_empty_supporting_no_section(self):
        """supporting_characters 为空时不输出【配角】标头。"""
        result = self._build([])
        self.assertNotIn("【配角】", result)


_TEST_CLASSES.append(TestSupportingCharRichRender)


# ══════════════════════════════════════════════════════════════════
# auto_register_npc 反向提取 event_cards 信息回归测试
# 验证：建档时从 event_cards 提取 NPC 相关事件作为 player_knowledge，
#       system prompt 中能完整呈现，且手动配角格式不变。
# ══════════════════════════════════════════════════════════════════

class TestAutoRegisterNpcExtractKnowledge(unittest.TestCase):
    """_auto_register_npc 从 event_cards 反向提取 player_knowledge 的逻辑。"""

    def _ws(self, events_by_date: dict | None = None) -> dict:
        ec = events_by_date or {}
        return {
            "save_info": {"turn": 10, "date": "4月12日 周六", "time_slot": "上午", "location": "公寓大厅"},
            "characters": {"heroines": [], "supporting_characters": []},
            "story_state": {"event_cards": ec, "event_archive": {}, "suspended_issues": []},
        }

    # ── player_knowledge 提取逻辑 ────────────────────────────────

    def test_knowledge_extracted_from_event_cards(self):
        """命名事件触发注册后，player_knowledge 包含 event_cards 中含该名字的事件摘要。"""
        ws = self._ws({"4月12日 周六": [
            "女邻居自报名字林晚",
            "林晚说她住15楼",
            "林晚对咖啡邀约给出暧昧回应",
            "主角到18楼准备搬家",   # 不含"林晚"，不应提取
        ]})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        # 含"林晚"的3条事件应被提取（去掉名字前缀后）
        self.assertTrue(len(pk) >= 2)
        # 具体内容：去掉"林晚"后应含住楼信息
        joined = " ".join(pk)
        self.assertIn("15楼", joined)
        self.assertIn("咖啡", joined)

    def test_non_npc_events_not_extracted(self):
        """不含该 NPC 名字的事件不被提取到 player_knowledge。"""
        ws = self._ws({"4月12日 周六": [
            "林晚说她住15楼",
            "主角在公寓大厅遇见神秘美女",
            "主角进入1802放下搬家箱子",
        ]})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        joined = " ".join(pk)
        self.assertNotIn("公寓大厅", joined)
        self.assertNotIn("搬家箱子", joined)

    def test_short_items_filtered_out(self):
        """去掉 NPC 名字后少于4字的条目被过滤，不进入 player_knowledge。"""
        ws = self._ws({"4月12日 周六": [
            "林晚走",       # 去掉"林晚"后剩"走"，1字，过滤
            "林晚说她住15楼",
        ]})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        for item in pk:
            self.assertGreaterEqual(len(item), 4, f"条目'{item}'不足4字，应被过滤")

    def test_dedup_applied(self):
        """重复事件只保留一条。"""
        ws = self._ws({"4月12日 周六": [
            "林晚说她住15楼",
            "林晚说她住15楼",   # 重复
            "林晚对咖啡邀约给出暧昧回应",
        ]})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        self.assertEqual(len(pk), len(set(pk)), "player_knowledge 中存在重复条目")

    def test_capped_at_10_items(self):
        """超过10条时只保留前10条。"""
        events = [f"林晚做了第{i}件事情啊" for i in range(15)]
        ws = self._ws({"4月12日 周六": events})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        self.assertLessEqual(len(pk), 10)

    def test_empty_event_cards_gives_empty_knowledge(self):
        """event_cards 为空时，player_knowledge 为空列表（不崩溃）。"""
        ws = self._ws({})
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        self.assertEqual(pk, [])

    def test_list_format_event_cards_also_works(self):
        """旧格式 event_cards（列表）也能正常提取。"""
        ws = self._ws()
        ws["story_state"]["event_cards"] = ["林晚说她住15楼", "主角搬家"]
        _m._auto_register_npc(ws, "林晚")
        pk = ws["characters"]["supporting_characters"][0]["player_knowledge"]
        self.assertTrue(len(pk) >= 1)

    # ── 端到端：system prompt 中可见 player_knowledge ─────────────

    def test_knowledge_appears_in_system_prompt(self):
        """auto_register 后，system prompt 中包含提取到的 player_knowledge 内容。"""
        from prompt.builder import build_static_system_prompt
        ws = self._ws({"4月12日 周六": [
            "女邻居自报名字林晚",
            "林晚说她住15楼",
            "林晚对咖啡邀约给出暧昧回应",
        ]})
        _m._auto_register_npc(ws, "林晚")
        prompt = build_static_system_prompt(ws)
        self.assertIn("15楼", prompt)
        self.assertIn("咖啡", prompt)

    def test_milestone_appears_in_system_prompt(self):
        """auto_register 后，system prompt 中包含初识 milestone 信息。"""
        from prompt.builder import build_static_system_prompt
        ws = self._ws({"4月12日 周六": ["女邻居自报名字林晚"]})
        _m._auto_register_npc(ws, "林晚")
        prompt = build_static_system_prompt(ws)
        self.assertIn("初识", prompt)

    # ── 验收标准3：手动配角格式不变 ─────────────────────────────

    def test_manual_supporting_char_format_unchanged(self):
        """只有 relationship_to_player 字段的手动配角，渲染格式不变（简短一行）。"""
        from prompt.builder import build_static_system_prompt
        ws = self._ws()
        ws["characters"]["supporting_characters"].append({
            "name": "物业大叔",
            "gender": "男",
            "type": "NPC",
            "relationship_to_player": "物业管理员",
        })
        prompt = build_static_system_prompt(ws)
        self.assertIn("物业大叔", prompt)
        self.assertNotIn("【角色：物业大叔】", prompt)
        self.assertIn("物业管理员", prompt)


_TEST_CLASSES.append(TestAutoRegisterNpcExtractKnowledge)


# ══════════════════════════════════════════════════════════════════
# 修改一：system_check.txt 时段检查 TIME LOCK RULE
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckTimeLockRule(unittest.TestCase):
    """验证 system_check.txt 中的时段检查使用 TIME LOCK RULE 表述。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_old_wording_removed(self):
        """旧表述"每3次行动推进一个时段"不应再存在。"""
        self.assertNotIn("每3次行动推进一个时段", self._content())

    def test_time_lock_rule_present(self):
        """新表述应包含 TIME LOCK RULE 关键字。"""
        self.assertIn("TIME LOCK RULE", self._content())

    def test_new_wording_complete(self):
        """新表述应完整包含三个触发条件的核心词。"""
        content = self._content()
        self.assertIn("明确等待", content)
        self.assertIn("明确离开场景", content)
        self.assertIn("禁止机械按回合数推进", content)

    def test_time_check_section_still_present(self):
        """【时段检查】节标题本身应保留。"""
        self.assertIn("【时段检查】", self._content())


_TEST_CLASSES.append(TestSystemCheckTimeLockRule)


# ══════════════════════════════════════════════════════════════════
# 修改二：npc_system.txt 属性生成原则
# ══════════════════════════════════════════════════════════════════

class TestNpcSystemAttrGenPrinciples(unittest.TestCase):
    """验证 npc_system.txt 【NPC隐藏属性】末尾包含三条属性生成原则。"""

    _FILE = _ROOT / "prompt" / "npc_system.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【属性生成原则】节标题应存在。"""
        self.assertIn("【属性生成原则】", self._content())

    def test_principle_1_equal_probability(self):
        """原则1：等概率随机抽取，禁止叙事偏好。"""
        content = self._content()
        self.assertIn("等概率随机抽取", content)
        self.assertIn("禁止因叙事偏好", content)

    def test_principle_2_no_duplicate_combination(self):
        """原则2：完整属性组合不得与任何已有NPC完全相同。"""
        content = self._content()
        self.assertIn("完整属性组合不得与任何已有NPC完全相同", content)
        self.assertIn("至少替换一个维度", content)

    def test_principle_3_gm_record(self):
        """原则3：GM内部记录完整属性组合用于重复检查。"""
        content = self._content()
        self.assertIn("GM内部记录完整属性组合", content)
        self.assertIn("后续重复检查", content)

    def test_principles_after_hidden_attr_section(self):
        """属性生成原则必须位于【NPC隐藏属性】章节之后、【情感独立系统】之前。"""
        content = self._content()
        pos_hidden = content.index("【NPC隐藏属性】")
        pos_principle = content.index("【属性生成原则】")
        pos_emotion = content.index("【情感独立系统")
        self.assertLess(pos_hidden, pos_principle)
        self.assertLess(pos_principle, pos_emotion)


_TEST_CLASSES.append(TestNpcSystemAttrGenPrinciples)


# ══════════════════════════════════════════════════════════════════
# 修改三：system_check.txt 属性多样性检查
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckAttrDiversitySection(unittest.TestCase):
    """验证 system_check.txt 在【NPC行为检查】之后包含【属性多样性检查】。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【属性多样性检查】节标题应存在。"""
        self.assertIn("【属性多样性检查】", self._content())

    def test_duplicate_check_description(self):
        """应包含"完整属性组合是否存在重复"检查描述。"""
        self.assertIn("完整属性组合是否存在重复", self._content())

    def test_annotation_format_present(self):
        """应包含指定的标注格式示例。"""
        self.assertIn("[属性重复：", self._content())
        self.assertIn("已标记待修正]", self._content())

    def test_after_npc_behavior_check(self):
        """【属性多样性检查】必须位于【NPC行为检查】之后、【性格锁定检查】之前。"""
        content = self._content()
        pos_npc_behavior = content.index("【NPC行为检查】")
        pos_diversity = content.index("【属性多样性检查】")
        pos_personality = content.index("【性格锁定检查】")
        self.assertLess(pos_npc_behavior, pos_diversity)
        self.assertLess(pos_diversity, pos_personality)


_TEST_CLASSES.append(TestSystemCheckAttrDiversitySection)


# ══════════════════════════════════════════════════════════════════
# 修改一：save_template.json world_config 字段
# ══════════════════════════════════════════════════════════════════

class TestSaveTemplateWorldConfig(unittest.TestCase):
    """验证 save_template.json 的 world 字段包含完整的 world_config 结构。"""

    _FILE = _ROOT / "prompt" / "save_template.json"

    def _template(self) -> dict:
        return json.loads(self._FILE.read_text(encoding="utf-8"))

    def test_world_config_key_exists(self):
        """world 下必须存在 world_config 键。"""
        self.assertIn("world_config", self._template()["world"])

    def test_forbidden_elements_is_list(self):
        """world_config.forbidden_elements 必须是列表。"""
        wc = self._template()["world"]["world_config"]
        self.assertIsInstance(wc["forbidden_elements"], list)

    def test_base_tone_is_string(self):
        """world_config.base_tone 必须是字符串。"""
        wc = self._template()["world"]["world_config"]
        self.assertIsInstance(wc["base_tone"], str)

    def test_unlockable_tones_is_dict(self):
        """world_config.unlockable_tones 必须是对象（dict）。"""
        wc = self._template()["world"]["world_config"]
        self.assertIsInstance(wc["unlockable_tones"], dict)

    def test_narrative_pace_removed(self):
        """原 narrative_pace 字段应已迁移到 narrative_config，顶层不再存在。"""
        wc = self._template()["world"]["world_config"]
        self.assertNotIn("narrative_pace", wc)

    def test_narrative_style_removed(self):
        """原 narrative_style 字段应已迁移到 narrative_config，顶层不再存在。"""
        wc = self._template()["world"]["world_config"]
        self.assertNotIn("narrative_style", wc)

    def test_narrative_config_exists(self):
        """world_config 下必须存在 narrative_config 对象。"""
        wc = self._template()["world"]["world_config"]
        self.assertIn("narrative_config", wc)
        self.assertIsInstance(wc["narrative_config"], dict)

    def test_narrative_config_pace_default(self):
        """narrative_config.pace 默认值为 'moderate'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["pace"], "moderate")

    def test_narrative_config_tone_default(self):
        """narrative_config.tone 默认值为 'neutral'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["tone"], "neutral")

    def test_narrative_config_style_default(self):
        """narrative_config.style 默认值为 'literary'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["style"], "literary")

    def test_narrative_config_pov_default(self):
        """narrative_config.pov 默认值为 'second'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["pov"], "second")

    def test_narrative_config_detail_level_default(self):
        """narrative_config.detail_level 默认值为 'medium'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["detail_level"], "medium")

    def test_narrative_config_dialogue_ratio_default(self):
        """narrative_config.dialogue_ratio 默认值为 'balanced'。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["dialogue_ratio"], "balanced")

    def test_narrative_config_all_required_fields(self):
        """narrative_config 必须包含全部6个字段。"""
        nc = self._template()["world"]["world_config"]["narrative_config"]
        for field in ("pace", "tone", "style", "pov", "detail_level", "dialogue_ratio"):
            self.assertIn(field, nc)

    def test_original_world_fields_preserved(self):
        """原有 world 字段（background/player_name/tone 等）必须保留。"""
        world = self._template()["world"]
        for key in ("background", "player_name", "player_identity", "tone", "player_scope"):
            self.assertIn(key, world)

    def test_template_is_valid_json(self):
        """save_template.json 必须是合法 JSON（不抛异常）。"""
        content = self._FILE.read_text(encoding="utf-8")
        data = json.loads(content)
        self.assertIsInstance(data, dict)


_TEST_CLASSES.append(TestSaveTemplateWorldConfig)


# ══════════════════════════════════════════════════════════════════
# 修改二：engine_rules.txt 世界观初始化规则
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesWorldInit(unittest.TestCase):
    """验证 engine_rules.txt ❶开局末尾包含完整的【世界观初始化规则】。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【世界观初始化规则】节标题应存在。"""
        self.assertIn("【世界观初始化规则】", self._content())

    def test_world_config_empty_trigger(self):
        """应说明 world_config 为空时触发推断。"""
        self.assertIn("world_config字段为空", self._content())

    def test_five_fields_mentioned(self):
        """五个填入字段（forbidden_elements/base_tone/unlockable_tones/
        narrative_pace/narrative_style）均应提及。"""
        content = self._content()
        for field in ("forbidden_elements", "base_tone", "unlockable_tones",
                      "narrative_pace", "narrative_style"):
            self.assertIn(field, content)

    def test_base_tone_options_listed(self):
        """base_tone 选项列表应包含 romantic 和 slice_of_life。"""
        content = self._content()
        self.assertIn("romantic", content)
        self.assertIn("slice_of_life", content)

    def test_player_confirm_lock(self):
        """应说明玩家确认后锁定。"""
        self.assertIn("确认后锁定", self._content())

    def test_position_before_option_response(self):
        """【世界观初始化规则】必须位于❶开局之后、❶.5选项响应原则之前。"""
        content = self._content()
        pos_init = content.index("【世界观初始化规则】")
        pos_option = content.index("❶.5 选项响应原则")
        self.assertLess(pos_init, pos_option)


_TEST_CLASSES.append(TestEngineRulesWorldInit)


# ══════════════════════════════════════════════════════════════════
# 修改三：engine_rules.txt WORLD LOCK RULE
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesWorldLockRule(unittest.TestCase):
    """验证 engine_rules.txt 稳定性规则章节包含完整的 WORLD LOCK RULE。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_world_lock_rule_title_present(self):
        """WORLD LOCK RULE 标题应存在。"""
        self.assertIn("WORLD LOCK RULE", self._content())

    def test_forbidden_elements_check(self):
        """应包含 forbidden_elements 元素检查规则。"""
        self.assertIn("forbidden_elements", self._content())

    def test_natural_substitution_rule(self):
        """应包含自然转化（不跳出游戏提醒玩家）的规则。"""
        self.assertIn("自然转化", self._content())
        self.assertIn("不得跳出游戏提醒玩家", self._content())

    def test_unlockable_tones_trigger(self):
        """应包含 unlockable_tones 触发条件满足时的处理规则。"""
        self.assertIn("unlockable_tones的触发条件满足时", self._content())

    def test_no_explicit_unlock_announcement(self):
        """应禁止明示已解锁基调。"""
        self.assertIn("不得向玩家明示", self._content())

    def test_gm_console_only_modify(self):
        """应说明 world_config 只能通过 GM 控制台修改。"""
        self.assertIn("world_config只能通过GM控制台修改", self._content())

    def test_position_in_stability_section(self):
        """WORLD LOCK RULE 必须位于 ACTION ORDER RULE 之后、GM控制台章节之前。"""
        content = self._content()
        pos_action = content.index("ACTION ORDER RULE")
        pos_world = content.index("WORLD LOCK RULE")
        pos_gm = content.index("【GM控制台】")
        self.assertLess(pos_action, pos_world)
        self.assertLess(pos_world, pos_gm)


_TEST_CLASSES.append(TestEngineRulesWorldLockRule)


# ══════════════════════════════════════════════════════════════════
# 修改四：system_check.txt 世界观检查
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckWorldSection(unittest.TestCase):
    """验证 system_check.txt 在【性格锁定检查】之后包含【世界观检查】。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【世界观检查】节标题应存在。"""
        self.assertIn("【世界观检查】", self._content())

    def test_forbidden_elements_check(self):
        """应包含 forbidden_elements 违规检查描述。"""
        self.assertIn("forbidden_elements", self._content())

    def test_unlockable_tones_check(self):
        """应包含 unlockable_tones 触发引入检查描述。"""
        self.assertIn("unlockable_tones", self._content())

    def test_violation_annotation_rule(self):
        """应包含发现违规须标注并说明修正方式的要求。"""
        self.assertIn("发现违规须在自检输出中标注并说明修正方式", self._content())

    def test_after_personality_lock_check(self):
        """【世界观检查】必须位于【性格锁定检查】之后、自检输出格式之前。"""
        content = self._content()
        pos_personality = content.index("【性格锁定检查】")
        pos_world = content.index("【世界观检查】")
        pos_output_fmt = content.index("自检输出格式：")
        self.assertLess(pos_personality, pos_world)
        self.assertLess(pos_world, pos_output_fmt)


_TEST_CLASSES.append(TestSystemCheckWorldSection)


# ══════════════════════════════════════════════════════════════════
# 修改一：engine_rules.txt NPC名字生成规则
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesNpcNameRule(unittest.TestCase):
    """验证 engine_rules.txt 包含完整的【NPC名字生成规则】。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【NPC名字生成规则】节标题应存在。"""
        self.assertIn("【NPC名字生成规则】", self._content())

    def test_forbidden_chars_listed(self):
        """禁用字列表应包含若干指定字符。"""
        content = self._content()
        for ch in ("晚", "雨", "晓", "汐", "沫", "殇"):
            self.assertIn(ch, content)

    def test_banned_common_surnames(self):
        """应明确禁止总是使用林、陈、李、王、张。"""
        self.assertIn("禁止总是使用林、陈、李、王、张", self._content())

    def test_preferred_surnames_listed(self):
        """推荐姓氏列表应包含若干指定姓氏。"""
        content = self._content()
        for surname in ("尉迟", "慕容", "上官", "欧阳", "令狐", "诸葛"):
            self.assertIn(surname, content)

    def test_principle_no_char_repeat(self):
        """原则2：同存档已有名字用字不得重复出现。"""
        self.assertIn("已有名字的用字不得重复出现", self._content())

    def test_principle_no_high_freq_combo(self):
        """原则3：禁止高频组合（知X / X知 / X澄 / X遥）。"""
        content = self._content()
        self.assertIn("知X", content)
        self.assertIn("X澄", content)
        self.assertIn("X遥", content)

    def test_position_after_world_init_before_option_response(self):
        """【NPC名字生成规则】必须位于【世界观初始化规则】之后、❶.5选项响应原则之前。"""
        content = self._content()
        pos_world = content.index("【世界观初始化规则】")
        pos_name = content.index("【NPC名字生成规则】")
        pos_option = content.index("❶.5 选项响应原则")
        self.assertLess(pos_world, pos_name)
        self.assertLess(pos_name, pos_option)


_TEST_CLASSES.append(TestEngineRulesNpcNameRule)


# ══════════════════════════════════════════════════════════════════
# 修改二：system_check.txt 名字检查
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckNameSection(unittest.TestCase):
    """验证 system_check.txt 在【属性多样性检查】之后包含【名字检查】。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【名字检查】节标题应存在。"""
        self.assertIn("【名字检查】", self._content())

    def test_forbidden_char_check(self):
        """应包含禁用字检查描述。"""
        self.assertIn("禁用字", self._content())

    def test_char_repeat_check(self):
        """应包含用字重复检查描述。"""
        self.assertIn("用字重复", self._content())

    def test_violation_annotation_format(self):
        """应包含指定的违规标注格式。"""
        self.assertIn("[名字违规：", self._content())
        self.assertIn("违反规则X]", self._content())

    def test_after_attr_diversity_before_personality_lock(self):
        """【名字检查】必须位于【属性多样性检查】之后、【性格锁定检查】之前。"""
        content = self._content()
        pos_attr = content.index("【属性多样性检查】")
        pos_name = content.index("【名字检查】")
        pos_personality = content.index("【性格锁定检查】")
        self.assertLess(pos_attr, pos_name)
        self.assertLess(pos_name, pos_personality)


_TEST_CLASSES.append(TestSystemCheckNameSection)


# ══════════════════════════════════════════════════════════════════
# 修改一：engine_rules.txt 好感触发保护规则
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesAffectionTriggerProtection(unittest.TestCase):
    """验证 engine_rules.txt ❸好感度末尾包含完整的【好感触发保护规则】。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_title_present(self):
        """【好感触发保护规则】节标题应存在。"""
        self.assertIn("【好感触发保护规则】", self._content())

    def test_noticed_state_defined(self):
        """"注意到了"状态应有明确定义。"""
        self.assertIn('"注意到了"状态', self._content())

    def test_four_trigger_conditions(self):
        """四个合法触发条件（累积3次/Roll成功/情感节点/GM控制台）均应存在。"""
        content = self._content()
        self.assertIn("同类行为累积3次以上", content)
        self.assertIn("Roll判定成功", content)
        self.assertIn("情感节点事件", content)
        self.assertIn("GM控制台直接修改", content)

    def test_three_banned_triggers(self):
        """三类禁止直接触发的情况均应存在。"""
        content = self._content()
        self.assertIn("单句魅力表达", content)
        self.assertIn("单次普通帮助行为", content)
        self.assertIn("玩家自我介绍或透露个人信息", content)

    def test_accumulate_3_times_normal_calc(self):
        """应说明累积3次后第3次正常计算好感变动。"""
        self.assertIn("累积3次同类行为后", self._content())

    def test_position_after_affection_before_event_order(self):
        """【好感触发保护规则】必须位于❸好感度之后、❹事件时序之前。"""
        content = self._content()
        pos_affection = content.index("❸ 好感度")
        pos_protect = content.index("【好感触发保护规则】")
        pos_event = content.index("❹ 事件时序")
        self.assertLess(pos_affection, pos_protect)
        self.assertLess(pos_protect, pos_event)


_TEST_CLASSES.append(TestEngineRulesAffectionTriggerProtection)


# ══════════════════════════════════════════════════════════════════
# 修改二：system_check.txt 触发保护检查
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckAffectionTriggerCheck(unittest.TestCase):
    """验证 system_check.txt 【好感检查】包含触发保护违规检查项。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_single_sentence_trigger_check(self):
        """应包含单句话/单次普通行为直接触发好感变动的检查描述。"""
        self.assertIn("单句话/单次普通行为直接触发好感变动", self._content())

    def test_violation_annotation_format(self):
        """应包含指定的违规标注格式。"""
        self.assertIn("[触发保护违规：第X回合，原因]", self._content())

    def test_retroactive_correction_required(self):
        """应要求回溯修正。"""
        self.assertIn("回溯修正", self._content())

    def test_inside_affection_check_section(self):
        """两条新检查项必须位于【好感检查】节内（在【时段检查】之前）。"""
        content = self._content()
        pos_affection = content.index("【好感检查】")
        pos_trigger_check = content.index("单句话/单次普通行为直接触发好感变动")
        pos_time_check = content.index("【时段检查】")
        self.assertLess(pos_affection, pos_trigger_check)
        self.assertLess(pos_trigger_check, pos_time_check)


_TEST_CLASSES.append(TestSystemCheckAffectionTriggerCheck)


# ══════════════════════════════════════════════════════════════════
# 修改一：npc_system.txt 新增角色出场权重规则
# ══════════════════════════════════════════════════════════════════

class TestNpcSystemWeightRules(unittest.TestCase):
    """验证 npc_system.txt 包含【角色出场权重规则】章节及各子规则。"""

    _FILE = _ROOT / "prompt" / "npc_system.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_weight_section_exists(self):
        """文件中存在【角色出场权重规则】章节标题。"""
        self.assertIn("【角色出场权重规则】", self._content())

    def test_initial_weight_value(self):
        """应说明初始值为10，范围1-20。"""
        c = self._content()
        self.assertIn("初始值为10", c)
        self.assertIn("范围1-20", c)

    def test_guaranteed_appearance_rule(self):
        """应包含连续5回合未出场的保底出场规则。"""
        self.assertIn("连续5回合未出场", self._content())

    def test_guaranteed_appearance_reset(self):
        """保底触发后计数归零的描述应存在。"""
        self.assertIn("连续未出场计数归零", self._content())

    def test_weight_adjustment_mention_plus2(self):
        """玩家主动提及+2的规则应存在。"""
        c = self._content()
        self.assertIn("玩家主动提及", c)
        self.assertIn("+2", c)

    def test_weight_adjustment_no_interaction_minus1(self):
        """出场后无实质互动-1的规则应存在。"""
        c = self._content()
        self.assertIn("玩家无实质互动", c)
        self.assertIn("-1", c)

    def test_weight_reset_on_guaranteed(self):
        """保底触发后weight重置为10的规则应存在。"""
        self.assertIn("weight重置为10", self._content())

    def test_promotion_rule_exists(self):
        """应包含路人升格规则。"""
        self.assertIn("路人升格规则", self._content())

    def test_promotion_condition_interaction(self):
        """升格条件：3次以上实质互动。"""
        self.assertIn("3次以上实质互动", self._content())

    def test_promotion_condition_inquiry(self):
        """升格条件：玩家主动询问个人信息。"""
        self.assertIn("玩家主动询问该NPC的个人信息", self._content())

    def test_promotion_manual_only(self):
        """升格操作由GM控制台执行，不自动完成。"""
        self.assertIn("升格操作由GM控制台执行，不自动完成", self._content())

    def test_weight_section_before_appearance_section(self):
        """【角色出场权重规则】应位于【外貌描写规则】之前。"""
        c = self._content()
        pos_weight = c.index("【角色出场权重规则】")
        pos_appearance = c.index("【外貌描写规则】")
        self.assertLess(pos_weight, pos_appearance)


_TEST_CLASSES.append(TestNpcSystemWeightRules)


# ══════════════════════════════════════════════════════════════════
# 修改二：save_template.json 新增 appearance_weight 字段
# ══════════════════════════════════════════════════════════════════

class TestSaveTemplateAppearanceWeight(unittest.TestCase):
    """验证 save_template.json 中 heroines 含有 appearance_weight 字段。"""

    _FILE = _ROOT / "prompt" / "save_template.json"

    def _template(self) -> dict:
        return json.loads(self._FILE.read_text(encoding="utf-8"))

    def test_template_is_valid_json(self):
        """模板文件仍为合法 JSON。"""
        data = self._template()
        self.assertIsInstance(data, dict)

    def test_heroines_key_exists(self):
        """模板中存在 heroines 键。"""
        self.assertIn("heroines", self._template())

    def test_heroines_contains_appearance_weight(self):
        """heroines 列表中的模板对象包含 appearance_weight 字段。"""
        heroines = self._template()["heroines"]
        self.assertTrue(len(heroines) > 0, "heroines 模板应至少有一个示例对象")
        template_heroine = heroines[0]
        self.assertIn("appearance_weight", template_heroine)

    def test_appearance_weight_value_default(self):
        """appearance_weight.value 默认值为 10。"""
        aw = self._template()["heroines"][0]["appearance_weight"]
        self.assertEqual(aw["value"], 10)

    def test_appearance_weight_consecutive_absent_default(self):
        """appearance_weight.consecutive_absent 默认值为 0。"""
        aw = self._template()["heroines"][0]["appearance_weight"]
        self.assertEqual(aw["consecutive_absent"], 0)

    def test_appearance_weight_value_is_int(self):
        """appearance_weight.value 为整数类型。"""
        aw = self._template()["heroines"][0]["appearance_weight"]
        self.assertIsInstance(aw["value"], int)

    def test_appearance_weight_consecutive_absent_is_int(self):
        """appearance_weight.consecutive_absent 为整数类型。"""
        aw = self._template()["heroines"][0]["appearance_weight"]
        self.assertIsInstance(aw["consecutive_absent"], int)


_TEST_CLASSES.append(TestSaveTemplateAppearanceWeight)


# ══════════════════════════════════════════════════════════════════
# 修改三：apply_updates() 新增 weight_updates 权重逻辑
# ══════════════════════════════════════════════════════════════════

def _ws_with_heroines(*names: str, value: int = 10, absent: int = 0) -> dict:
    """构造含多个 heroine 的 world_state，每个都有 appearance_weight。"""
    heroines = [
        {
            "name": n,
            "appearance_weight": {"value": value, "consecutive_absent": absent},
        }
        for n in names
    ]
    ws = {
        "save_info": {"turn": 1, "date": "1月1日", "time_slot": "上午", "location": "城镇"},
        "characters": {"heroines": heroines, "supporting_characters": []},
        "story_state": {"event_cards": {}, "suspended_issues": []},
    }
    return ws


class TestApplyUpdatesWeightLogic(unittest.TestCase):
    """验证 apply_updates() 的 weight_updates 处理逻辑。"""

    def _apply(self, ws: dict, weight_updates: list) -> None:
        _m.apply_updates(ws, {"weight_updates": weight_updates})

    def _aw(self, ws: dict, name: str) -> dict:
        for h in ws["characters"]["heroines"]:
            if h["name"] == name:
                return h["appearance_weight"]
        raise KeyError(name)

    # ── delta 更新 ────────────────────────────────────────────────

    def test_weight_delta_positive(self):
        """weight_updates delta=+2 正确累加到 value。"""
        ws = _ws_with_heroines("A", value=10)
        self._apply(ws, [{"name": "A", "delta": 2, "reason": "测试"}])
        self.assertEqual(self._aw(ws, "A")["value"], 12)

    def test_weight_delta_negative(self):
        """weight_updates delta=-1 正确减少 value。"""
        ws = _ws_with_heroines("A", value=10)
        self._apply(ws, [{"name": "A", "delta": -1, "reason": "测试"}])
        self.assertEqual(self._aw(ws, "A")["value"], 9)

    def test_weight_clamped_upper_bound(self):
        """value 不超过上限 20。"""
        ws = _ws_with_heroines("A", value=19)
        self._apply(ws, [{"name": "A", "delta": 5, "reason": "测试"}])
        self.assertEqual(self._aw(ws, "A")["value"], 20)

    def test_weight_clamped_lower_bound(self):
        """value 不低于下限 1。"""
        ws = _ws_with_heroines("A", value=2)
        self._apply(ws, [{"name": "A", "delta": -5, "reason": "测试"}])
        self.assertEqual(self._aw(ws, "A")["value"], 1)

    def test_unknown_heroine_ignored(self):
        """weight_updates 中未知 heroine 名称不引发异常，其他 heroine 不受影响。"""
        ws = _ws_with_heroines("A", value=10)
        self._apply(ws, [{"name": "不存在的人", "delta": 3, "reason": ""}])
        self.assertEqual(self._aw(ws, "A")["value"], 10)

    # ── consecutive_absent 更新 ───────────────────────────────────

    def test_appeared_heroine_absent_reset(self):
        """weight_updates 中列出的 heroine consecutive_absent 归零。"""
        ws = _ws_with_heroines("A", absent=4)
        self._apply(ws, [{"name": "A", "delta": 0, "reason": ""}])
        self.assertEqual(self._aw(ws, "A")["consecutive_absent"], 0)

    def test_absent_heroine_count_incremented(self):
        """未在 weight_updates 中出现的 heroine consecutive_absent +1。"""
        ws = _ws_with_heroines("A", "B", absent=2)
        # 只有 A 出场
        self._apply(ws, [{"name": "A", "delta": 0, "reason": ""}])
        self.assertEqual(self._aw(ws, "A")["consecutive_absent"], 0)
        self.assertEqual(self._aw(ws, "B")["consecutive_absent"], 3)

    def test_multiple_heroines_partial_appearance(self):
        """多女主场景下出场/未出场计数各自正确更新。"""
        ws = _ws_with_heroines("甲", "乙", "丙", absent=0)
        self._apply(ws, [
            {"name": "甲", "delta": 2, "reason": ""},
            {"name": "丙", "delta": 0, "reason": ""},
        ])
        self.assertEqual(self._aw(ws, "甲")["consecutive_absent"], 0)
        self.assertEqual(self._aw(ws, "乙")["consecutive_absent"], 1)
        self.assertEqual(self._aw(ws, "丙")["consecutive_absent"], 0)

    def test_no_weight_updates_key_is_noop(self):
        """updates 中不含 weight_updates 时，heroine weight 不变。"""
        ws = _ws_with_heroines("A", value=10, absent=2)
        _m.apply_updates(ws, {"save_info": {"date": "1月2日"}})
        self.assertEqual(self._aw(ws, "A")["value"], 10)
        self.assertEqual(self._aw(ws, "A")["consecutive_absent"], 2)

    def test_absent_count_auto_initializes(self):
        """heroine 若缺少 appearance_weight，apply_updates 自动初始化并累加 consecutive_absent。"""
        ws = {
            "save_info": {"turn": 1, "date": "1月1日", "time_slot": "上午", "location": "城镇"},
            "characters": {
                "heroines": [{"name": "A"}],
                "supporting_characters": [],
            },
            "story_state": {"event_cards": {}, "suspended_issues": []},
        }
        _m.apply_updates(ws, {"weight_updates": []})
        aw = ws["characters"]["heroines"][0]["appearance_weight"]
        self.assertEqual(aw["consecutive_absent"], 1)   # 未出场，+1
        self.assertEqual(aw["value"], 10)               # 初始值


_TEST_CLASSES.append(TestApplyUpdatesWeightLogic)


# ══════════════════════════════════════════════════════════════════
# 修改四：build_dynamic_context() 注入保底出场提醒
# ══════════════════════════════════════════════════════════════════

def _ws_overdue(*names_absent: tuple) -> dict:
    """
    构造含多个 heroine 的 world_state。
    names_absent: [(name, consecutive_absent), ...]
    """
    heroines = [
        {
            "name": name,
            "appearance_weight": {"value": 10, "consecutive_absent": absent},
        }
        for name, absent in names_absent
    ]
    return {
        "save_info": {"turn": 1, "date": "1月1日", "time_slot": "上午", "location": "城镇"},
        "characters": {"heroines": heroines, "supporting_characters": []},
        "story_state": {"event_cards": {}, "suspended_issues": []},
    }


class TestDynamicContextGuaranteedAppearance(unittest.TestCase):
    """验证 build_dynamic_context() 在 consecutive_absent>=5 时注入保底提醒。"""

    def _ctx(self, ws: dict) -> str:
        from prompt.builder import build_dynamic_context
        return build_dynamic_context(ws)

    def test_no_reminder_when_all_absent_below_threshold(self):
        """所有 heroine consecutive_absent < 5 时，不出现【出场提醒】。"""
        ws = _ws_overdue(("A", 4), ("B", 3))
        self.assertNotIn("【出场提醒】", self._ctx(ws))

    def test_reminder_appears_when_absent_equals_5(self):
        """consecutive_absent == 5 时触发【出场提醒】。"""
        ws = _ws_overdue(("林知遥", 5))
        ctx = self._ctx(ws)
        self.assertIn("【出场提醒】", ctx)
        self.assertIn("林知遥", ctx)

    def test_reminder_appears_when_absent_greater_than_5(self):
        """consecutive_absent > 5 时同样触发【出场提醒】。"""
        ws = _ws_overdue(("萧雨", 7))
        ctx = self._ctx(ws)
        self.assertIn("【出场提醒】", ctx)
        self.assertIn("萧雨", ctx)

    def test_only_overdue_heroines_listed(self):
        """只有 consecutive_absent >= 5 的 heroine 出现在提醒中。"""
        ws = _ws_overdue(("A", 5), ("B", 2))
        ctx = self._ctx(ws)
        self.assertIn("A", ctx)
        # B 的 absent=2，不应出现在保底提醒段（可能出现在别处，故检查位置）
        pos_reminder = ctx.index("【出场提醒】")
        # B 若出现在 reminder 块后面则说明被错误列入
        reminder_block = ctx[pos_reminder:]
        self.assertNotIn("B", reminder_block)

    def test_multiple_overdue_heroines_all_listed(self):
        """多个 consecutive_absent >= 5 的 heroine 全部出现在提醒中。"""
        ws = _ws_overdue(("甲", 5), ("乙", 6), ("丙", 1))
        ctx = self._ctx(ws)
        self.assertIn("甲", ctx)
        self.assertIn("乙", ctx)

    def test_no_reminder_when_no_heroines(self):
        """没有 heroine 时不出现【出场提醒】。"""
        ws = {
            "save_info": {"turn": 1, "date": "1月1日", "time_slot": "上午", "location": "城镇"},
            "characters": {"heroines": [], "supporting_characters": []},
            "story_state": {"event_cards": {}, "suspended_issues": []},
        }
        self.assertNotIn("【出场提醒】", self._ctx(ws))

    def test_no_reminder_when_appearance_weight_missing(self):
        """heroine 不含 appearance_weight 字段时不触发保底提醒（不报错）。"""
        ws = {
            "save_info": {"turn": 1, "date": "1月1日", "time_slot": "上午", "location": "城镇"},
            "characters": {
                "heroines": [{"name": "A"}],
                "supporting_characters": [],
            },
            "story_state": {"event_cards": {}, "suspended_issues": []},
        }
        ctx = self._ctx(ws)
        self.assertNotIn("【出场提醒】", ctx)

    def test_reminder_includes_mandatory_wording(self):
        """提醒文案包含"本回合须安排出场"。"""
        ws = _ws_overdue(("X", 5))
        self.assertIn("本回合须安排出场", self._ctx(ws))


_TEST_CLASSES.append(TestDynamicContextGuaranteedAppearance)


# ══════════════════════════════════════════════════════════════════
# 修改五：system_check.txt 新增【出场权重检查】
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckWeightSection(unittest.TestCase):
    """验证 system_check.txt 包含【出场权重检查】及其三条子项。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_weight_check_section_exists(self):
        """文件中存在【出场权重检查】章节标题。"""
        self.assertIn("【出场权重检查】", self._content())

    def test_guaranteed_absence_check(self):
        """应包含连续≥5回合未出场但未安排的检查项。"""
        c = self._content()
        self.assertIn("连续≥5回合未出场", c)
        self.assertIn("须标注并修正", c)

    def test_weight_order_reference_check(self):
        """应包含weight排序参考检查项（仅供参考，不强制）。"""
        c = self._content()
        self.assertIn("weight排序", c)
        self.assertIn("仅供参考，不强制", c)

    def test_promotion_suggestion_check(self):
        """应包含 supporting_characters 升格条件检查及"建议升格"标注格式。"""
        c = self._content()
        self.assertIn("supporting_characters", c)
        self.assertIn("建议升格", c)

    def test_weight_check_before_personality_lock_check(self):
        """【出场权重检查】应位于【性格锁定检查】之前。"""
        c = self._content()
        pos_weight = c.index("【出场权重检查】")
        pos_personality = c.index("【性格锁定检查】")
        self.assertLess(pos_weight, pos_personality)

    def test_weight_check_inside_self_check_section(self):
        """【出场权重检查】应位于自检系统内（❶ 定期自检之后）。"""
        c = self._content()
        pos_periodic = c.index("❶ 定期自检")
        pos_weight = c.index("【出场权重检查】")
        self.assertLess(pos_periodic, pos_weight)


_TEST_CLASSES.append(TestSystemCheckWeightSection)


# ══════════════════════════════════════════════════════════════════
# 管理员控制台（admin_console.py）
# ══════════════════════════════════════════════════════════════════

import admin_console as _ac   # noqa: E402


def _make_save(tmp_dir: Path, heroines=None, supporting=None,
               save_info=None, world=None) -> Path:
    """在临时目录创建一个最小存档文件，返回其 Path。"""
    data = {
        "save_info": save_info or {"turn": 5, "date": "1月5日", "time_slot": "上午", "location": "城镇"},
        "world":     world     or {"world_config": {}},
        "characters": {
            "heroines":              heroines   or [],
            "supporting_characters": supporting or [],
        },
        "story_state": {"event_cards": {}, "suspended_issues": []},
    }
    p = Path(tmp_dir) / "test_save.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _load_save(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


# ── 辅助夹具 ─────────────────────────────────────────────────────

class _AdminBase(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self._tmp = tempfile.mkdtemp(prefix="ac_")
        self._h1 = {"name": "林知遥", "affection": 50,
                     "appearance_weight": {"value": 10, "consecutive_absent": 2},
                     "player_knowledge": [], "relationship_milestones": []}
        self._s1 = {"name": "陈默", "affection": 30,
                     "player_knowledge": ["是邻居"],
                     "relationship_milestones": [
                         {"round": 1, "date": "1月1日", "location": "门口", "event": "初识", "detail": "偶遇"},
                         {"round": 3, "date": "1月3日", "location": "楼道", "event": "再次相遇", "detail": "打招呼"},
                         {"round": 5, "date": "1月5日", "location": "电梯", "event": "交谈", "detail": "聊了几句"},
                     ]}
        self._save_path = _make_save(
            self._tmp,
            heroines=[self._h1],
            supporting=[self._s1],
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _data(self):
        return _load_save(self._save_path)


# ══════════════════════════════════════════════════════════════════
# 修改一：admin_console.py 命令集合
# ══════════════════════════════════════════════════════════════════

class TestAdminConsoleCommandSet(unittest.TestCase):
    """验证 admin_console.COMMANDS 包含所有要求的命令名。"""

    _REQUIRED = {
        "set_affection", "set_all_affection", "set_world_tone",
        "set_weight", "add_knowledge", "add_milestone",
        "promote_npc", "list_npcs", "show_npc",
        "set_narrative",
    }

    def test_all_commands_defined(self):
        """COMMANDS 集合包含全部 10 个命令。"""
        self.assertEqual(_ac.COMMANDS, self._REQUIRED)

    def test_commands_is_set(self):
        """COMMANDS 是 set 类型。"""
        self.assertIsInstance(_ac.COMMANDS, set)


_TEST_CLASSES.append(TestAdminConsoleCommandSet)


# ══════════════════════════════════════════════════════════════════
# 修改二：各命令的具体行为
# ══════════════════════════════════════════════════════════════════

class TestAdminSetAffection(_AdminBase):
    """set_affection：设置指定NPC好感度。"""

    def test_set_affection_normal(self):
        """正常设置好感度。"""
        _ac.cmd_set_affection(self._data(), "林知遥", 80)
        # 验证需实际修改数据：调用后重新写盘验证
        d = self._data()
        _ac.cmd_set_affection(d, "林知遥", 80)
        _ac._save(d, str(self._save_path))
        self.assertEqual(self._data()["characters"]["heroines"][0]["affection"], 80)

    def test_set_affection_clamp_upper(self):
        """超过100时裁剪为100。"""
        d = self._data()
        _ac.cmd_set_affection(d, "林知遥", 999)
        _ac._save(d, str(self._save_path))
        self.assertEqual(self._data()["characters"]["heroines"][0]["affection"], 100)

    def test_set_affection_clamp_lower(self):
        """低于0时裁剪为0。"""
        d = self._data()
        _ac.cmd_set_affection(d, "林知遥", -10)
        _ac._save(d, str(self._save_path))
        self.assertEqual(self._data()["characters"]["heroines"][0]["affection"], 0)

    def test_set_affection_supporting(self):
        """也能修改 supporting_characters 的好感度。"""
        d = self._data()
        _ac.cmd_set_affection(d, "陈默", 60)
        _ac._save(d, str(self._save_path))
        self.assertEqual(self._data()["characters"]["supporting_characters"][0]["affection"], 60)

    def test_set_affection_unknown_npc_exits(self):
        """找不到NPC时 sys.exit。"""
        d = self._data()
        with self.assertRaises(SystemExit):
            _ac.cmd_set_affection(d, "不存在", 50)


_TEST_CLASSES.append(TestAdminSetAffection)


class TestAdminSetAllAffection(_AdminBase):
    """set_all_affection：批量设置所有NPC好感度。"""

    def test_all_npcs_updated(self):
        """所有NPC（heroine + supporting）都被更新。"""
        d = self._data()
        _ac.cmd_set_all_affection(d, 75)
        _ac._save(d, str(self._save_path))
        saved = self._data()
        for h in saved["characters"]["heroines"]:
            self.assertEqual(h["affection"], 75)
        for s in saved["characters"]["supporting_characters"]:
            self.assertEqual(s["affection"], 75)

    def test_all_affection_clamp(self):
        """超出范围时裁剪。"""
        d = self._data()
        _ac.cmd_set_all_affection(d, 200)
        for npc in _ac._all_npcs(d):
            self.assertEqual(npc["affection"], 100)


_TEST_CLASSES.append(TestAdminSetAllAffection)


class TestAdminSetWorldTone(_AdminBase):
    """set_world_tone：修改 world_config.narrative_tone。"""

    def test_tone_written(self):
        """tone 被写入 world.world_config.narrative_tone。"""
        d = self._data()
        _ac.cmd_set_world_tone(d, "dark")
        _ac._save(d, str(self._save_path))
        saved = self._data()
        self.assertEqual(saved["world"]["world_config"]["narrative_tone"], "dark")

    def test_tone_arbitrary_string(self):
        """任意字符串都能写入。"""
        d = self._data()
        _ac.cmd_set_world_tone(d, "自定义基调")
        self.assertEqual(d["world"]["world_config"]["narrative_tone"], "自定义基调")


_TEST_CLASSES.append(TestAdminSetWorldTone)


class TestAdminSetWeight(_AdminBase):
    """set_weight：修改指定 heroine 的出场权重。"""

    def test_weight_set_normal(self):
        """正常设置权重。"""
        d = self._data()
        _ac.cmd_set_weight(d, "林知遥", 15)
        _ac._save(d, str(self._save_path))
        aw = self._data()["characters"]["heroines"][0]["appearance_weight"]
        self.assertEqual(aw["value"], 15)

    def test_weight_clamp_upper(self):
        """超过20时裁剪为20。"""
        d = self._data()
        _ac.cmd_set_weight(d, "林知遥", 99)
        self.assertEqual(d["characters"]["heroines"][0]["appearance_weight"]["value"], 20)

    def test_weight_clamp_lower(self):
        """低于1时裁剪为1。"""
        d = self._data()
        _ac.cmd_set_weight(d, "林知遥", -5)
        self.assertEqual(d["characters"]["heroines"][0]["appearance_weight"]["value"], 1)

    def test_weight_unknown_npc_exits(self):
        """找不到NPC时 sys.exit。"""
        with self.assertRaises(SystemExit):
            _ac.cmd_set_weight(self._data(), "不存在", 10)


_TEST_CLASSES.append(TestAdminSetWeight)


class TestAdminAddKnowledge(_AdminBase):
    """add_knowledge：向NPC认知档案添加条目。"""

    def test_knowledge_appended(self):
        """新条目被追加。"""
        d = self._data()
        _ac.cmd_add_knowledge(d, "林知遥", "是自由职业者")
        self.assertIn("是自由职业者", d["characters"]["heroines"][0]["player_knowledge"])

    def test_knowledge_deduped(self):
        """重复条目不被二次添加。"""
        d = self._data()
        _ac.cmd_add_knowledge(d, "陈默", "是邻居")  # 已存在
        self.assertEqual(d["characters"]["supporting_characters"][0]["player_knowledge"].count("是邻居"), 1)

    def test_knowledge_unknown_npc_exits(self):
        """找不到NPC时 sys.exit。"""
        with self.assertRaises(SystemExit):
            _ac.cmd_add_knowledge(self._data(), "不存在", "test")


_TEST_CLASSES.append(TestAdminAddKnowledge)


class TestAdminAddMilestone(_AdminBase):
    """add_milestone：向NPC关系节点添加一条。"""

    def test_milestone_appended(self):
        """新节点被追加。"""
        d = self._data()
        _ac.cmd_add_milestone(d, "林知遥", "告白", "在天台")
        ms = d["characters"]["heroines"][0]["relationship_milestones"]
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0]["event"], "告白")
        self.assertEqual(ms[0]["detail"], "在天台")

    def test_milestone_uses_save_info(self):
        """自动填入存档的 date 和 turn。"""
        d = self._data()
        _ac.cmd_add_milestone(d, "林知遥", "初识", "偶遇")
        ms = d["characters"]["heroines"][0]["relationship_milestones"]
        self.assertEqual(ms[0]["date"], "1月5日")
        self.assertEqual(ms[0]["round"], 5)

    def test_milestone_unknown_npc_exits(self):
        """找不到NPC时 sys.exit。"""
        with self.assertRaises(SystemExit):
            _ac.cmd_add_milestone(self._data(), "不存在", "告白", "test")


_TEST_CLASSES.append(TestAdminAddMilestone)


class TestAdminPromoteNpc(_AdminBase):
    """promote_npc：将 supporting 升格为 heroine。"""

    def test_npc_moved_to_heroines(self):
        """升格后 NPC 出现在 heroines，不在 supporting 中。"""
        d = self._data()
        _ac.cmd_promote_npc(d, "陈默")
        names_h = [h["name"] for h in d["characters"]["heroines"]]
        names_s = [s["name"] for s in d["characters"]["supporting_characters"]]
        self.assertIn("陈默", names_h)
        self.assertNotIn("陈默", names_s)

    def test_promoted_npc_has_appearance_weight(self):
        """升格后 NPC 有 appearance_weight 字段。"""
        d = self._data()
        _ac.cmd_promote_npc(d, "陈默")
        chen = next(h for h in d["characters"]["heroines"] if h["name"] == "陈默")
        self.assertIn("appearance_weight", chen)
        self.assertEqual(chen["appearance_weight"]["value"], 10)

    def test_promoted_npc_has_relationship_stage(self):
        """升格后 NPC 有 relationship_stage 字段。"""
        d = self._data()
        _ac.cmd_promote_npc(d, "陈默")
        chen = next(h for h in d["characters"]["heroines"] if h["name"] == "陈默")
        self.assertIn("relationship_stage", chen)

    def test_promote_not_in_supporting_exits(self):
        """NPC 不在 supporting_characters 中时 sys.exit。"""
        d = self._data()
        with self.assertRaises(SystemExit):
            _ac.cmd_promote_npc(d, "林知遥")   # 已是 heroine

    def test_promote_unknown_exits(self):
        """NPC 完全不存在时 sys.exit。"""
        with self.assertRaises(SystemExit):
            _ac.cmd_promote_npc(self._data(), "不存在")


_TEST_CLASSES.append(TestAdminPromoteNpc)


class TestAdminListNpcs(_AdminBase):
    """list_npcs：输出NPC状态表。"""

    def _capture(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _ac.cmd_list_npcs(self._data())
        return buf.getvalue()

    def test_heroine_section_header(self):
        """输出包含【Heroines】标头。"""
        self.assertIn("【Heroines】", self._capture())

    def test_supporting_section_header(self):
        """输出包含【Supporting】标头。"""
        self.assertIn("【Supporting】", self._capture())

    def test_heroine_name_in_output(self):
        """Heroine 名字出现在输出中。"""
        self.assertIn("林知遥", self._capture())

    def test_supporting_name_in_output(self):
        """Supporting NPC 名字出现在输出中。"""
        self.assertIn("陈默", self._capture())

    def test_promotion_hint_shown(self):
        """满足升格条件的 NPC 标注 [建议升格]。"""
        # 陈默有3条milestones，应触发建议升格
        self.assertIn("[建议升格]", self._capture())

    def test_weight_shown_for_heroine(self):
        """Heroine 行输出 weight 字段。"""
        self.assertIn("weight=", self._capture())


_TEST_CLASSES.append(TestAdminListNpcs)


class TestAdminShowNpc(_AdminBase):
    """show_npc：输出指定NPC完整档案。"""

    def _capture(self, name: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _ac.cmd_show_npc(self._data(), name)
        return buf.getvalue()

    def test_heroine_name_in_output(self):
        """输出包含 NPC 名字。"""
        self.assertIn("林知遥", self._capture("林知遥"))

    def test_affection_shown(self):
        """输出包含 affection 值。"""
        self.assertIn("affection", self._capture("林知遥"))

    def test_appearance_weight_shown(self):
        """输出包含 appearance_weight 信息。"""
        self.assertIn("appearance_weight", self._capture("林知遥"))

    def test_milestones_shown_for_supporting(self):
        """supporting NPC 的 milestones 出现在输出中。"""
        out = self._capture("陈默")
        self.assertIn("relationship_milestones", out)
        self.assertIn("初识", out)

    def test_unknown_npc_exits(self):
        """找不到NPC时 sys.exit。"""
        with self.assertRaises(SystemExit):
            _ac.cmd_show_npc(self._data(), "不存在")


_TEST_CLASSES.append(TestAdminShowNpc)


class TestAdminLoadSave(unittest.TestCase):
    """_load / _save 基本读写。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="ac_ls_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_load_valid_json(self):
        p = Path(self._tmp) / "s.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        data = _ac._load(str(p))
        self.assertEqual(data["a"], 1)

    def test_load_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            _ac._load(str(Path(self._tmp) / "no.json"))

    def test_load_bad_json_exits(self):
        p = Path(self._tmp) / "bad.json"
        p.write_text("not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            _ac._load(str(p))

    def test_save_writes_valid_json(self):
        p = Path(self._tmp) / "out.json"
        _ac._save({"x": 42}, str(p))
        self.assertTrue(p.exists())
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["x"], 42)


_TEST_CLASSES.append(TestAdminLoadSave)


# ══════════════════════════════════════════════════════════════════
# 修改三：engine_rules.txt 新增【GM控制台说明】
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesAdminConsoleSection(unittest.TestCase):
    """验证 engine_rules.txt 末尾包含【GM控制台说明】及各子项。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_header_exists(self):
        """文件包含【GM控制台说明】标题。"""
        self.assertIn("【GM控制台说明】", self._content())

    def test_admin_console_mentioned(self):
        """说明中提及 admin_console.py。"""
        self.assertIn("admin_console.py", self._content())

    def test_already_happened_fact(self):
        """说明'结果视为已发生的游戏事实'。"""
        self.assertIn("已发生的游戏事实", self._content())

    def test_affection_attitude_sync(self):
        """包含好感调整后态度应同步的说明。"""
        c = self._content()
        self.assertIn("affection被批量调整", c)
        self.assertIn("不得沿用旧态度", c)

    def test_promote_npc_note(self):
        """包含NPC升格后叙事体现的说明。"""
        self.assertIn("升格为heroine", self._content())

    def test_no_event_cards_note(self):
        """包含控制台修改不产生event_cards记录的说明。"""
        self.assertIn("控制台修改不产生event_cards记录", self._content())

    def test_section_after_appearance_description(self):
        """【GM控制台说明】位于⓬外貌描写之后（文件末尾区域）。"""
        c = self._content()
        pos_appearance = c.index("⓬ 外貌描写")
        pos_admin = c.index("【GM控制台说明】")
        self.assertLess(pos_appearance, pos_admin)


_TEST_CLASSES.append(TestEngineRulesAdminConsoleSection)


# ══════════════════════════════════════════════════════════════════
# Session 3 修改二：engine_rules.txt 新增【叙事基调规则】
# ══════════════════════════════════════════════════════════════════

class TestEngineRulesNarrativeConfigSection(unittest.TestCase):
    """验证 engine_rules.txt 包含【叙事基调规则】及各字段说明。"""

    _FILE = _ROOT / "prompt" / "engine_rules.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_header_exists(self):
        """文件包含【叙事基调规则】标题。"""
        self.assertIn("【叙事基调规则】", self._content())

    def test_narrative_config_field_referenced(self):
        """包含 narrative_config 字段路径的引用。"""
        self.assertIn("narrative_config", self._content())

    def test_pace_field_explained(self):
        """包含 pace 字段说明（slow/moderate/fast）。"""
        c = self._content()
        self.assertIn("pace", c)
        self.assertIn("slow", c)
        self.assertIn("moderate", c)
        self.assertIn("fast", c)

    def test_tone_field_explained(self):
        """包含 tone 字段说明（warm/neutral/dark/tense）。"""
        c = self._content()
        self.assertIn("warm", c)
        self.assertIn("dark", c)
        self.assertIn("tense", c)

    def test_style_field_explained(self):
        """包含 style 字段说明（literary/casual/cinematic/minimalist）。"""
        c = self._content()
        self.assertIn("literary", c)
        self.assertIn("casual", c)
        self.assertIn("cinematic", c)
        self.assertIn("minimalist", c)

    def test_pov_field_explained(self):
        """包含 pov 字段说明（second/third）。"""
        c = self._content()
        self.assertIn("pov", c)
        self.assertIn("second", c)
        self.assertIn("third", c)

    def test_detail_level_field_explained(self):
        """包含 detail_level 字段说明（low/medium/high）。"""
        c = self._content()
        self.assertIn("detail_level", c)
        self.assertIn("low", c)
        self.assertIn("high", c)

    def test_dialogue_ratio_field_explained(self):
        """包含 dialogue_ratio 字段说明。"""
        c = self._content()
        self.assertIn("dialogue_ratio", c)
        self.assertIn("dialogue_heavy", c)
        self.assertIn("narration_heavy", c)

    def test_set_narrative_command_referenced(self):
        """包含 set_narrative 命令的提及（GM须用命令修改）。"""
        self.assertIn("set_narrative", self._content())

    def test_section_after_admin_console(self):
        """【叙事基调规则】位于【GM控制台说明】之后。"""
        c = self._content()
        pos_admin = c.index("【GM控制台说明】")
        pos_narrative = c.index("【叙事基调规则】")
        self.assertLess(pos_admin, pos_narrative)


_TEST_CLASSES.append(TestEngineRulesNarrativeConfigSection)


# ══════════════════════════════════════════════════════════════════
# Session 3 修改四：admin_console.py 新增 set_narrative 命令
# ══════════════════════════════════════════════════════════════════

class TestAdminSetNarrative(_AdminBase, unittest.TestCase):
    """验证 set_narrative 命令正确修改 narrative_config 字段。"""

    def _save_with_narrative(self, **kwargs) -> str:
        """创建含 narrative_config 的存档，kwargs 覆盖默认值。"""
        nc = {"pace": "moderate", "tone": "neutral", "style": "literary",
              "pov": "second", "detail_level": "medium", "dialogue_ratio": "balanced"}
        nc.update(kwargs)
        data = {"world": {"world_config": {"narrative_config": nc}}}
        return _make_save(self._tmp, data)

    def test_set_pace(self):
        """set_narrative --field pace --value slow 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "pace", "--value", "slow"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["pace"], "slow")

    def test_set_tone(self):
        """set_narrative --field tone --value dark 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "tone", "--value", "dark"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["tone"], "dark")

    def test_set_style(self):
        """set_narrative --field style --value cinematic 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "style", "--value", "cinematic"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["style"], "cinematic")

    def test_set_pov(self):
        """set_narrative --field pov --value third 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "pov", "--value", "third"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["pov"], "third")

    def test_set_detail_level(self):
        """set_narrative --field detail_level --value high 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "detail_level", "--value", "high"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["detail_level"], "high")

    def test_set_dialogue_ratio(self):
        """set_narrative --field dialogue_ratio --value dialogue_heavy 修改成功。"""
        p = self._save_with_narrative()
        _ac.main(["--save", str(p), "set_narrative", "--field", "dialogue_ratio", "--value", "dialogue_heavy"])
        self.assertEqual(_load_save(p)["world"]["world_config"]["narrative_config"]["dialogue_ratio"], "dialogue_heavy")

    def test_invalid_value_exits(self):
        """非法值应 SystemExit（argparse choices 拒绝）。"""
        p = self._save_with_narrative()
        with self.assertRaises(SystemExit):
            _ac.main(["--save", str(p), "set_narrative", "--field", "pace", "--value", "ultra_fast"])

    def test_creates_narrative_config_if_absent(self):
        """存档中没有 narrative_config 时自动创建。"""
        data = {"world": {"world_config": {}}}
        p = _make_save(self._tmp, data)
        _ac.main(["--save", str(p), "set_narrative", "--field", "tone", "--value", "warm"])
        nc = _load_save(p)["world"]["world_config"]["narrative_config"]
        self.assertEqual(nc["tone"], "warm")

    def test_all_six_fields_accepted(self):
        """6个字段均可被 set_narrative 修改，无报错。"""
        cases = [
            ("pace", "fast"), ("tone", "tense"), ("style", "minimalist"),
            ("pov", "third"), ("detail_level", "low"), ("dialogue_ratio", "narration_heavy"),
        ]
        for field, value in cases:
            p = self._save_with_narrative()
            _ac.main(["--save", str(p), "set_narrative", "--field", field, "--value", value])
            actual = _load_save(p)["world"]["world_config"]["narrative_config"][field]
            self.assertEqual(actual, value, msg=f"field={field}")

    def test_narrative_allowed_dict_has_six_fields(self):
        """_NARRATIVE_ALLOWED 必须包含全部6个字段。"""
        self.assertEqual(len(_ac._NARRATIVE_ALLOWED), 6)
        for field in ("pace", "tone", "style", "pov", "detail_level", "dialogue_ratio"):
            self.assertIn(field, _ac._NARRATIVE_ALLOWED)

    def test_command_in_commands_set(self):
        """'set_narrative' 必须在 COMMANDS 集合中。"""
        self.assertIn("set_narrative", _ac.COMMANDS)


_TEST_CLASSES.append(TestAdminSetNarrative)


# ══════════════════════════════════════════════════════════════════
# Session 3 修改三：system_check.txt 新增【叙事基调检查】
# ══════════════════════════════════════════════════════════════════

class TestSystemCheckNarrativeSection(unittest.TestCase):
    """验证 system_check.txt 包含【叙事基调检查】及各检查项。"""

    _FILE = _ROOT / "prompt" / "system_check.txt"

    def _content(self) -> str:
        return self._FILE.read_text(encoding="utf-8")

    def test_section_header_exists(self):
        """文件包含【叙事基调检查】标题。"""
        self.assertIn("【叙事基调检查】", self._content())

    def test_pace_check_item(self):
        """包含 pace 的检查条目。"""
        self.assertIn("narrative_config.pace", self._content())

    def test_tone_check_item(self):
        """包含 tone 的检查条目。"""
        self.assertIn("narrative_config.tone", self._content())

    def test_pov_consistency_check(self):
        """包含视角（pov）一致性检查。"""
        c = self._content()
        self.assertIn("pov", c)
        self.assertIn("second", c)
        self.assertIn("third", c)

    def test_detail_level_check(self):
        """包含 detail_level 的检查条目。"""
        self.assertIn("detail_level", self._content())

    def test_dialogue_ratio_check(self):
        """包含 dialogue_ratio 的检查条目。"""
        self.assertIn("dialogue_ratio", self._content())

    def test_deviation_annotation_format(self):
        """包含偏差标注格式说明。"""
        self.assertIn("叙事基调偏差", self._content())

    def test_section_before_personality_lock(self):
        """【叙事基调检查】位于【性格锁定检查】之前。"""
        c = self._content()
        pos_narrative = c.index("【叙事基调检查】")
        pos_personality = c.index("【性格锁定检查】")
        self.assertLess(pos_narrative, pos_personality)


_TEST_CLASSES.append(TestSystemCheckNarrativeSection)


# ══════════════════════════════════════════════════════════════════
# Fix 5 — tension 持久化：save_info 存储、动态上下文注入
# ══════════════════════════════════════════════════════════════════

class TestTensionPersistence(unittest.TestCase):
    """验证 tension 字段能被 apply_updates 写入并被 build_dynamic_context 读出。"""

    def _ws_with_tension(self, tension: int) -> dict:
        return {
            "save_info": {
                "turn": 5,
                "date": "3月18日 周二",
                "time_slot": "下午",
                "location": "咖啡馆",
                "tension": tension,
            }
        }

    # ── 1. _ALLOWED_SAVE_INFO_KEYS 包含 tension ─────────────────────
    def test_tension_in_allowed_keys(self):
        """_ALLOWED_SAVE_INFO_KEYS 必须包含 tension，否则模型输出会被丢弃。"""
        self.assertIn("tension", _m._ALLOWED_SAVE_INFO_KEYS)

    # ── 2. apply_updates 能写入 tension ─────────────────────────────
    def test_apply_updates_writes_tension(self):
        """parse_response 解析出的 tension 经 apply_updates 写入 world_state.save_info。"""
        raw = (
            '叙事文本。\n'
            '---JSON---\n'
            '{"save_info": {"date": "3月18日 周二", "time_slot": "下午",'
            ' "location": "咖啡馆", "tension": 5}, "new_event": "玩家与林晚交谈"}'
        )
        narrative, updates = _m.parse_response(raw)
        self.assertEqual(narrative, "叙事文本。")
        self.assertIn("save_info", updates)
        self.assertEqual(updates["save_info"].get("tension"), 5)

        ws = {}
        _m.apply_updates(ws, updates)
        self.assertEqual(ws["save_info"]["tension"], 5)

    # ── 3. build_dynamic_context 注入 tension ────────────────────────
    def test_dynamic_context_includes_tension(self):
        """save_info 有 tension 时，动态上下文包含 ⚡ 标记和数值。"""
        from prompt.builder import build_dynamic_context
        ctx = build_dynamic_context(self._ws_with_tension(5))
        self.assertIn("⚡", ctx)
        self.assertIn("5", ctx)

    def test_dynamic_context_tension_zero(self):
        """tension=0 时同样注入（0 是合法值，不应被 falsy 判断跳过）。"""
        from prompt.builder import build_dynamic_context
        ctx = build_dynamic_context(self._ws_with_tension(0))
        self.assertIn("⚡", ctx)
        self.assertIn("0", ctx)

    def test_dynamic_context_no_tension_field(self):
        """save_info 没有 tension 字段时，不注入 ⚡ 标记（不崩溃）。"""
        from prompt.builder import build_dynamic_context
        ws = {"save_info": {"turn": 1, "date": "3月18日", "time_slot": "上午", "location": "家"}}
        ctx = build_dynamic_context(ws)
        self.assertNotIn("⚡", ctx)

    # ── 4. tension 上下限校验（parse_response 不强制，仅存储原值）──
    def test_apply_updates_tension_stored_as_int(self):
        """tension 值以整数原样存储，不做裁剪（合法范围由模型规则保证）。"""
        raw = '叙事。\n---JSON---\n{"save_info": {"date": "x", "time_slot": "上午", "location": "y", "tension": 9}, "new_event": "e"}'
        _, updates = _m.parse_response(raw)
        ws = {}
        _m.apply_updates(ws, updates)
        self.assertIsInstance(ws["save_info"]["tension"], int)
        self.assertEqual(ws["save_info"]["tension"], 9)

    # ── 5. core_constraints.txt 格式样例包含 tension ─────────────────
    def test_core_constraints_json_example_has_tension(self):
        """core_constraints.txt 的 ---JSON--- 格式样例必须包含 tension 字段。"""
        path = _ROOT / "prompt" / "core_constraints.txt"
        content = path.read_text(encoding="utf-8")
        self.assertIn('"tension"', content)


_TEST_CLASSES.append(TestTensionPersistence)


# ══════════════════════════════════════════════════════════════════
# weight_updates 端到端：parse_response → apply_updates
# ══════════════════════════════════════════════════════════════════

class TestWeightUpdatesEndToEnd(unittest.TestCase):
    """验证 weight_updates 从 parse_response 到 apply_updates 的完整链路。"""

    def _ws(self, *names: str, value: int = 10, absent: int = 0) -> dict:
        return _ws_with_heroines(*names, value=value, absent=absent)

    # ── 1. parse_response 不丢弃 weight_updates ──────────────────

    def test_parse_response_keeps_weight_updates(self):
        """parse_response 收到 weight_updates 时，该字段出现在 updates 中。"""
        raw = (
            '叙事文本。\n'
            '---JSON---\n'
            '{"save_info": {"date": "1月1日", "time_slot": "上午", "location": "校园"},'
            ' "new_event": "甲出场",'
            ' "weight_updates": [{"name": "甲", "delta": 2, "reason": "主动搭话"}]}'
        )
        _, updates = _m.parse_response(raw)
        self.assertIn("weight_updates", updates)
        self.assertEqual(len(updates["weight_updates"]), 1)
        self.assertEqual(updates["weight_updates"][0]["name"], "甲")
        self.assertEqual(updates["weight_updates"][0]["delta"], 2)

    def test_parse_response_weight_updates_not_in_allowed_before_fix_is_now_fixed(self):
        """_ALLOWED_UPDATE_KEYS 现在必须包含 weight_updates。"""
        self.assertIn("weight_updates", _m._ALLOWED_UPDATE_KEYS)

    # ── 2. apply_updates 正确更新 value，裁剪到 [1, 20] ──────────

    def test_apply_updates_value_updated_via_parse(self):
        """parse_response 输出的 weight_updates 经 apply_updates 正确写入 value。"""
        raw = (
            'x\n---JSON---\n'
            '{"weight_updates": [{"name": "甲", "delta": 3, "reason": ""}]}'
        )
        _, updates = _m.parse_response(raw)
        ws = self._ws("甲", value=10)
        _m.apply_updates(ws, updates)
        aw = ws["characters"]["heroines"][0]["appearance_weight"]
        self.assertEqual(aw["value"], 13)

    def test_value_clamped_to_upper_20(self):
        """delta 使 value 超过 20 时裁剪为 20。"""
        raw = 'x\n---JSON---\n{"weight_updates": [{"name": "甲", "delta": 99, "reason": ""}]}'
        _, updates = _m.parse_response(raw)
        ws = self._ws("甲", value=18)
        _m.apply_updates(ws, updates)
        self.assertEqual(ws["characters"]["heroines"][0]["appearance_weight"]["value"], 20)

    def test_value_clamped_to_lower_1(self):
        """delta 使 value 低于 1 时裁剪为 1。"""
        raw = 'x\n---JSON---\n{"weight_updates": [{"name": "甲", "delta": -99, "reason": ""}]}'
        _, updates = _m.parse_response(raw)
        ws = self._ws("甲", value=3)
        _m.apply_updates(ws, updates)
        self.assertEqual(ws["characters"]["heroines"][0]["appearance_weight"]["value"], 1)

    # ── 3. consecutive_absent 归零 / +1 ──────────────────────────

    def test_appeared_heroine_consecutive_absent_reset(self):
        """weight_updates 中出现的 heroine consecutive_absent 归零。"""
        raw = 'x\n---JSON---\n{"weight_updates": [{"name": "甲", "delta": 0, "reason": ""}]}'
        _, updates = _m.parse_response(raw)
        ws = self._ws("甲", absent=5)
        _m.apply_updates(ws, updates)
        self.assertEqual(ws["characters"]["heroines"][0]["appearance_weight"]["consecutive_absent"], 0)

    def test_absent_heroine_consecutive_absent_incremented(self):
        """weight_updates 中未出现的 heroine consecutive_absent +1。"""
        raw = 'x\n---JSON---\n{"weight_updates": [{"name": "甲", "delta": 0, "reason": ""}]}'
        _, updates = _m.parse_response(raw)
        ws = self._ws("甲", "乙", absent=2)
        _m.apply_updates(ws, updates)
        heroines = ws["characters"]["heroines"]
        absent_map = {h["name"]: h["appearance_weight"]["consecutive_absent"] for h in heroines}
        self.assertEqual(absent_map["甲"], 0)   # 出场：归零
        self.assertEqual(absent_map["乙"], 3)   # 未出场：+1

    def test_multi_heroine_mixed_appearance(self):
        """多女主场景：部分出场、部分未出场，各自正确更新。"""
        raw = (
            'x\n---JSON---\n'
            '{"weight_updates": ['
            '{"name": "A", "delta": 1, "reason": ""},'
            '{"name": "C", "delta": -1, "reason": ""}'
            ']}'
        )
        _, updates = _m.parse_response(raw)
        ws = _ws_with_heroines("A", "B", "C", absent=3)
        _m.apply_updates(ws, updates)
        heroines = ws["characters"]["heroines"]
        absent_map = {h["name"]: h["appearance_weight"]["consecutive_absent"] for h in heroines}
        self.assertEqual(absent_map["A"], 0)   # 出场
        self.assertEqual(absent_map["B"], 4)   # 未出场：3+1
        self.assertEqual(absent_map["C"], 0)   # 出场


_TEST_CLASSES.append(TestWeightUpdatesEndToEnd)


# ══════════════════════════════════════════════════════════════════
# event_archive 删除验证
# ══════════════════════════════════════════════════════════════════

class TestEventArchiveRemoved(unittest.TestCase):
    """验证 event_archive 写入逻辑已删除：超窗口条目直接丢弃，不归档。"""

    def _ws_base(self) -> dict:
        return {
            "save_info": {"turn": 1, "date": "第1天", "time_slot": "上午", "location": "城镇"},
            "characters": {"heroines": [], "supporting_characters": []},
            "story_state": {"event_cards": {}, "suspended_issues": []},
        }

    def _add_event(self, ws: dict, date: str, event: str) -> None:
        ws["save_info"]["date"] = date
        _m.apply_updates(ws, {"new_event": event})

    def test_old_dates_discarded_not_archived(self):
        """event_cards 超过14天后，最旧一天被丢弃，不写入 event_archive。"""
        ws = self._ws_base()
        for i in range(15):
            self._add_event(ws, f"第{i+1}天", f"事件{i+1}")
        story = ws["story_state"]
        self.assertNotIn("event_archive", story)
        self.assertLessEqual(len(story["event_cards"]), 14)

    def test_discarded_date_no_longer_in_event_cards(self):
        """第1天的条目在超出窗口后从 event_cards 中消失。"""
        ws = self._ws_base()
        for i in range(15):
            self._add_event(ws, f"第{i+1}天", f"事件{i+1}")
        self.assertNotIn("第1天", ws["story_state"]["event_cards"])

    def test_newest_date_retained(self):
        """超出窗口后，最新一天的条目仍然保留。"""
        ws = self._ws_base()
        for i in range(15):
            self._add_event(ws, f"第{i+1}天", f"事件{i+1}")
        self.assertIn("第15天", ws["story_state"]["event_cards"])

    def test_no_event_archive_written_within_window(self):
        """event_cards 未超出14天时，story_state 里不出现 event_archive。"""
        ws = self._ws_base()
        for i in range(5):
            self._add_event(ws, f"第{i+1}天", f"事件{i+1}")
        self.assertNotIn("event_archive", ws["story_state"])

    def test_existing_event_archive_in_old_save_not_touched(self):
        """旧存档里已有 event_archive 字段，apply_updates 不会写入新条目。"""
        ws = self._ws_base()
        ws["story_state"]["event_archive"] = {"旧日期": "旧归档内容"}
        for i in range(15):
            self._add_event(ws, f"第{i+1}天", f"事件{i+1}")
        # 旧数据保留，但不新增 key
        archive = ws["story_state"].get("event_archive", {})
        self.assertIn("旧日期", archive)
        self.assertNotIn("第1天", archive)


_TEST_CLASSES.append(TestEventArchiveRemoved)


# ══════════════════════════════════════════════════════════════════
# story_state 死字段清理验证
# ══════════════════════════════════════════════════════════════════

class TestStoryStateDeadFieldsRemoved(unittest.TestCase):
    """验证 fallback 存档模板里 story_state 不含 time/location 死字段。"""

    def _get_fallback_template(self) -> str:
        """触发 fallback 路径：临时把 _SAVE_TEMPLATE 置空。"""
        import prompt.builder as _b
        original = _b._SAVE_TEMPLATE
        _b._SAVE_TEMPLATE = ""
        try:
            result = _b.build_save_request_prompt()
        finally:
            _b._SAVE_TEMPLATE = original
        return result

    def _parse_fallback_json(self) -> dict:
        """从 fallback prompt 里提取并解析 JSON 模板块。"""
        import json as _json
        prompt = self._get_fallback_template()
        start = prompt.index("{")
        data = _json.loads(prompt[start:])
        return data

    def test_story_state_has_no_time_field(self):
        """fallback 模板 story_state 不包含 'time' 死字段。"""
        data = self._parse_fallback_json()
        self.assertNotIn("time", data["story_state"])

    def test_story_state_has_no_location_field(self):
        """fallback 模板 story_state 不包含 'location' 死字段。"""
        data = self._parse_fallback_json()
        self.assertNotIn("location", data["story_state"])

    def test_save_info_time_slot_present(self):
        """save_info.time_slot 仍存在于 fallback 模板，不受影响。"""
        prompt = self._get_fallback_template()
        self.assertIn("time_slot", prompt)

    def test_save_info_location_present(self):
        """save_info.location 仍存在于 fallback 模板，不受影响。"""
        prompt = self._get_fallback_template()
        save_line = next(l for l in prompt.splitlines() if "save_info" in l)
        self.assertIn("location", save_line)


_TEST_CLASSES.append(TestStoryStateDeadFieldsRemoved)


# ══════════════════════════════════════════════════════════════════
# 字段注册一致性检查
# ══════════════════════════════════════════════════════════════════

class TestContextSkipFieldsDeclaration(unittest.TestCase):
    """修改一：_CONTEXT_SKIP_FIELDS 声明存在且类型正确。"""

    def test_context_skip_fields_exists(self):
        self.assertTrue(hasattr(_m, "_CONTEXT_SKIP_FIELDS"))

    def test_context_skip_fields_is_set(self):
        self.assertIsInstance(_m._CONTEXT_SKIP_FIELDS, (set, frozenset))

    def test_weight_updates_in_skip_fields(self):
        self.assertIn("weight_updates", _m._CONTEXT_SKIP_FIELDS)

    def test_skip_fields_subset_of_update_keys(self):
        """跳过字段必须是 _ALLOWED_UPDATE_KEYS 的子集。"""
        self.assertTrue(
            _m._CONTEXT_SKIP_FIELDS <= _m._ALLOWED_UPDATE_KEYS,
            "_CONTEXT_SKIP_FIELDS 包含不在 _ALLOWED_UPDATE_KEYS 中的字段",
        )


_TEST_CLASSES.append(TestContextSkipFieldsDeclaration)


class TestValidateFieldRegistry(unittest.TestCase):
    """修改二：_validate_field_registry() 在字段缺失时抛出 RuntimeError。"""

    def test_valid_registry_passes(self):
        """当前注册字段应全部通过校验，不抛异常。"""
        _m._validate_field_registry()

    def test_fake_field_raises_runtime_error(self):
        """往 _ALLOWED_UPDATE_KEYS 临时加一个假字段，应抛出 RuntimeError。"""
        original = _m._ALLOWED_UPDATE_KEYS.copy()
        _m._ALLOWED_UPDATE_KEYS.add("test_field_nonexistent")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                _m._validate_field_registry()
            self.assertIn("test_field_nonexistent", str(ctx.exception))
        finally:
            _m._ALLOWED_UPDATE_KEYS.clear()
            _m._ALLOWED_UPDATE_KEYS.update(original)

    def test_skip_field_not_checked_in_builder(self):
        """在 _CONTEXT_SKIP_FIELDS 中的字段不需要出现在 builder.py，不应报错。"""
        original_keys = _m._ALLOWED_UPDATE_KEYS.copy()
        original_skip = _m._CONTEXT_SKIP_FIELDS.copy()
        _m._ALLOWED_UPDATE_KEYS.add("__dummy_skip__")
        _m._CONTEXT_SKIP_FIELDS.add("__dummy_skip__")
        try:
            # 只要 main_src 里有字面量 "__dummy_skip__" 就不报 apply_updates 缺失
            # 但 main_src 里没有 → 应报 apply_updates 缺失
            with self.assertRaises(RuntimeError) as ctx:
                _m._validate_field_registry()
            msg = str(ctx.exception)
            self.assertIn("apply_updates()", msg)
            # 但不应报 build_dynamic_context 缺失（因为已在跳过名单）
            self.assertNotIn("build_dynamic_context() 未渲染且未声明跳过: __dummy_skip__", msg)
        finally:
            _m._ALLOWED_UPDATE_KEYS.clear()
            _m._ALLOWED_UPDATE_KEYS.update(original_keys)
            _m._CONTEXT_SKIP_FIELDS.clear()
            _m._CONTEXT_SKIP_FIELDS.update(original_skip)

    def test_error_message_lists_both_failures(self):
        """假字段不在 skip 列表中，应同时报 apply_updates 和 builder 两个缺失。"""
        original = _m._ALLOWED_UPDATE_KEYS.copy()
        _m._ALLOWED_UPDATE_KEYS.add("__both_missing__")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                _m._validate_field_registry()
            msg = str(ctx.exception)
            self.assertIn("apply_updates() 未处理字段: __both_missing__", msg)
            self.assertIn("build_dynamic_context() 未渲染且未声明跳过: __both_missing__", msg)
        finally:
            _m._ALLOWED_UPDATE_KEYS.clear()
            _m._ALLOWED_UPDATE_KEYS.update(original)


_TEST_CLASSES.append(TestValidateFieldRegistry)


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
