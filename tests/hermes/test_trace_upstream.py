"""钉住干活轨迹（``wonton/trace.py``）依赖的上游形状（Hermes 0.23.4）：观察钩子的名字和参数、
ACP 适配器给工具分类和写预览的函数、日志用的脱敏，以及走完 ``trace._upstream`` 的说明里不留密钥。

这几个函数不是插件接口；升级 Hermes 时这里先红，按新形状改 ``trace._upstream``。
"""

import inspect
import sys
import unittest
from pathlib import Path

from acp_adapter import tools as acp_tools
from agent import display
from hermes_cli import plugins, plugins_dispatch
import model_tools

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wonton import trace  # noqa: E402

SECRET = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


class TraceUpstreamCase(unittest.TestCase):
    def test_the_hooks_we_observe_exist(self):
        for hook in ("pre_tool_call", "post_tool_call", "pre_approval_request", "post_approval_response",
                     "on_session_end", "pre_gateway_dispatch"):
            self.assertIn(hook, plugins.VALID_HOOKS)

    def test_pre_tool_call_is_fail_closed_so_our_hook_must_not_raise(self):
        # 回调抛错或超时，上游拦下这次工具调用：trace 的钩子自己接住所有异常。
        self.assertIn("pre_tool_call", plugins_dispatch._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS)

    def test_post_tool_call_says_whether_the_tool_failed(self):
        source = inspect.getsource(model_tools._emit_post_tool_call_hook)
        self.assertIn("status=status", source)
        self.assertIn("tool_call_id", inspect.getsource(model_tools._CallIds))
        self.assertIn("session_id", inspect.getsource(model_tools._CallIds))
        self.assertEqual(model_tools._tool_result_observer_fields("terminal", '{"error": "boom"}')[0], "error")
        self.assertEqual(model_tools._tool_result_observer_fields("terminal", '{"output": "ok"}')[0], "ok")

    def test_tools_map_to_acp_kinds(self):
        self.assertEqual(acp_tools.get_tool_kind("read_file"), "read")
        self.assertEqual(acp_tools.get_tool_kind("terminal"), "execute")
        self.assertEqual(acp_tools.get_tool_kind("web_search"), "fetch")
        self.assertEqual(acp_tools.get_tool_kind("some_plugin_tool"), "other")

    def test_the_preview_is_one_short_line(self):
        self.assertEqual(list(inspect.signature(display.build_tool_preview).parameters)[:3],
                         ["tool_name", "args", "max_len"])
        self.assertEqual(display.build_tool_preview("read_file", {"path": "notes.md"}, max_len=80), "notes.md")
        long = display.build_tool_preview("terminal", {"command": "echo " + "x" * 500}, max_len=80)
        self.assertLessEqual(len(long), 80)
        self.assertNotIn("\n", display.build_tool_preview("terminal", {"command": "a\nb"}, max_len=80))

    def test_the_title_we_send_has_no_secrets(self):
        # 上游的预览不脱敏终端命令；trace 自己再过一遍日志的脱敏。
        command = f"curl -H 'Authorization: Bearer {SECRET}' https://example.com"
        self.assertIn(SECRET, display.build_tool_preview("terminal", {"command": command}, max_len=200) or "")
        kind, preview = trace._upstream("terminal", {"command": command}, with_preview=True)
        self.assertEqual(kind, "execute")
        self.assertTrue(preview.startswith("curl"))
        self.assertNotIn(SECRET, preview)
        _, preview = trace._upstream("terminal", {"command": f"OPENAI_API_KEY={SECRET} ./run"}, with_preview=True)
        self.assertNotIn(SECRET, preview)
        self.assertEqual(trace._upstream("terminal", {"command": command}, with_preview=False), ("execute", None))
        # 密钥落在 80 个字符的截断处也认得出来：先脱敏再截短。
        _, preview = trace._upstream("terminal", {"command": "x" * 60 + f" --token={SECRET}"}, with_preview=True)
        self.assertNotIn(SECRET[:12], preview)
        self.assertLessEqual(len(preview), trace.PREVIEW_LEN)


if __name__ == "__main__":
    unittest.main()
