"""干活轨迹的行为测试：给定钩子调用序列（调工具、工具返回、上游审批、一轮结束、重启），断言发进房间的状态消息和编辑。
事件 JSON 按 ADR-0128 的格式做契约断言。

Matrix 适配器是假的，上游的类别和预览函数换成固定的表（真的那两个在 tests/hermes/test_trace_upstream.py 里钉住）；
语言固定为中文。
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from wonton import trace, turn  # noqa: E402

OWNER = "@owner:example.org"
ROOM = "!dm1:example.org"
REQUEST = "$request1"
SID = "session-1"

KINDS = {"read_file": "read", "terminal": "execute", "tool_call": "other"}
PREVIEWS = {"read_file": "notes.md", "terminal": "ls -la"}


def upstream(tool_name, args, with_preview):
    return KINDS.get(tool_name, "other"), PREVIEWS.get(tool_name) if with_preview else None


class Source:
    def __init__(self, chat_type):
        self.chat_type = chat_type


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.sent = []
        self.fail = False

    async def _send_room_message(self, room, content):
        if self.fail:
            raise RuntimeError("连不上")
        self.sent.append((room, content))
        return f"$status{len(self.sent)}"


async def zh_language(_user_id=None):
    return "zh-Hans"


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = FakeAdapter()
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        for key, value in (("runs", {}), ("inflight", set()), ("recovered", False)):
            trace._S[key] = value
        self.saved = {key: turn._S[key] for key in ("loop", "active", "origins")}
        self.addCleanup(lambda: turn._S.update(self.saved))
        turn._S.update({"loop": asyncio.get_running_loop(), "active": {}, "origins": {}})
        self.patches = [
            mock.patch.dict(os.environ, {"HERMES_HOME": self.home.name}),
            mock.patch.object(trace, "language", zh_language),
            mock.patch.object(trace, "_adapter", lambda: self.adapter),
            mock.patch.object(trace, "_upstream", upstream),
            mock.patch.object(trace, "EDIT_INTERVAL", 0.01),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    async def asyncTearDown(self):
        await trace._drain()

    def granted(self, chat_type="dm", sid=SID):
        """核心准了这一轮（turn 在 pre_llm_call 里记下的样子）。"""
        turn._S["active"][sid] = {"sender": OWNER, "session": sid, "room": ROOM, "thread": None}
        turn._S["origins"][sid] = {"sender": OWNER, "room": ROOM, "thread": None,
                                   "event_id": REQUEST, "source": Source(chat_type)}

    async def settle(self):
        """等发送协程把攒下的变化发完（编辑间隔在测试里是 10 毫秒）。"""
        for _ in range(20):
            await asyncio.sleep(0.02)

    def last(self):
        """房间里这条状态消息现在的内容：最后一次编辑的 m.new_content，没编辑过就是第一条。"""
        content = self.adapter.sent[-1][1]
        return content.get("m.new_content", content)

    async def test_a_dm_round_traces_each_call_and_ends_done(self):
        self.granted()
        trace.pre_tool_call(tool_name="read_file", args={"path": "notes.md"}, session_id=SID, tool_call_id="c1")
        await self.settle()
        first_room, first = self.adapter.sent[0]
        self.assertEqual(first_room, ROOM)
        self.assertEqual(first["msgtype"], "m.notice")
        self.assertEqual(first["org.fluxchat.agent"],
                         {"v": 1, "state": "working", "steps": 1, "run": REQUEST, "step": "读文件"})
        self.assertEqual(first["app.wonton.trace"], {"v": 1, "calls": [
            {"id": "c1", "kind": "read", "title": "read_file: notes.md", "status": "in_progress"}]})
        self.assertEqual(first["body"], "读文件（第 1 步）")

        trace.post_tool_call(session_id=SID, tool_call_id="c1", status="ok")
        trace.pre_tool_call(tool_name="terminal", args={"command": "ls -la"}, session_id=SID, tool_call_id="c2")
        trace.post_tool_call(session_id=SID, tool_call_id="c2", status="error")
        trace.on_session_end(session_id=SID, completed=True)
        await trace._drain()

        edit = self.adapter.sent[-1][1]
        self.assertEqual(edit["m.relates_to"], {"rel_type": "m.replace", "event_id": "$status1"})
        self.assertEqual(edit["body"], "* 完成，用了 2 步")
        final = self.last()
        self.assertEqual(final["org.fluxchat.agent"]["state"], "done")
        self.assertEqual(final["org.fluxchat.agent"]["steps"], 2)
        self.assertEqual(final["app.wonton.trace"]["calls"], [
            {"id": "c1", "kind": "read", "title": "read_file: notes.md", "status": "completed"},
            {"id": "c2", "kind": "execute", "title": "terminal: ls -la", "status": "failed"},
        ])
        self.assertNotIn("app.wonton.end", final)
        self.assertNotIn("m.relates_to", final, "新内容里不带关系")
        # 结束了就从重启收尾的记录里删掉。
        self.assertFalse(Path(self.home.name, trace.OPEN_FILE).exists())

    async def test_the_first_message_already_lists_the_first_call(self):
        # 钩子在工具线程里跑：发送协程可能在第一项记进清单之前就开始了，不能先发一条“第 0 步”。
        self.granted()
        trace._run_for(SID)
        await self.settle()
        self.assertEqual(self.adapter.sent, [])
        trace.pre_tool_call(tool_name="terminal", args={"command": "ls -la"}, session_id=SID, tool_call_id="c1")
        await self.settle()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.sent[0][1]["org.fluxchat.agent"]["steps"], 1)
        trace.on_session_end(session_id=SID, completed=True)

    async def test_a_group_room_only_names_the_kind(self):
        self.granted(chat_type="group")
        trace.pre_tool_call(tool_name="terminal", args={"command": "cat ~/secret.txt"}, session_id=SID,
                            tool_call_id="c1")
        trace.on_session_end(session_id=SID, completed=True)
        await trace._drain()
        calls = self.last()["app.wonton.trace"]["calls"]
        self.assertEqual(calls, [{"id": "c1", "kind": "execute", "title": "执行命令", "status": "in_progress"}])
        self.assertNotIn("secret", json.dumps([content for _, content in self.adapter.sent], ensure_ascii=False))

    async def test_a_round_without_tools_sends_nothing(self):
        self.granted()
        trace.on_session_end(session_id=SID, completed=True)
        await trace._drain()
        self.assertEqual(self.adapter.sent, [])

    async def test_a_round_the_core_did_not_grant_is_not_traced(self):
        # 网页控制台、定时任务、子代理和被拒的一轮：turn 没把它记成进行中的 Matrix 一轮。
        turn._S["active"][SID] = {"sender": OWNER, "session": SID, "room": None, "thread": None}
        trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID, tool_call_id="c1")
        trace.pre_tool_call(tool_name="terminal", args={}, session_id="other", tool_call_id="c2")
        trace.on_session_end(session_id=SID, completed=True)
        await trace._drain()
        self.assertEqual(self.adapter.sent, [])

    async def test_interrupted_is_failed_with_cancelled_and_errors_say_so(self):
        self.granted()
        trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID, tool_call_id="c1")
        trace.on_session_end(session_id=SID, interrupted=True)
        await trace._drain()
        final = self.last()
        self.assertEqual(final["org.fluxchat.agent"]["state"], "failed")
        self.assertEqual(final["app.wonton.end"], "cancelled")
        self.assertEqual(final["body"], "打断了")

        self.adapter.sent.clear()
        self.granted(sid="s2")
        trace.pre_tool_call(tool_name="terminal", args={}, session_id="s2", tool_call_id="c1")
        trace.on_session_end(session_id="s2", failed=True)
        await trace._drain()
        final = self.last()
        self.assertEqual(final["org.fluxchat.agent"]["state"], "failed")
        self.assertEqual(final["org.fluxchat.agent"]["result"], "这一轮出错了")
        self.assertNotIn("app.wonton.end", final)

    async def test_an_upstream_approval_waits_then_works_again(self):
        self.granted()
        trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID, tool_call_id="c1")
        trace.pre_approval_request(session_id=SID, command="rm -rf build")
        await self.settle()
        self.assertEqual(self.last()["org.fluxchat.agent"]["state"], "waiting")
        self.assertEqual(self.last()["body"], "等你确认")
        trace.post_approval_response(session_id=SID, choice="once")
        await self.settle()
        self.assertEqual(self.last()["org.fluxchat.agent"]["state"], "working")
        trace.on_session_end(session_id=SID, completed=True)

    async def test_the_edit_cap_leaves_room_for_the_final_edit(self):
        self.granted()
        with mock.patch.object(trace, "MAX_EDITS", 3):
            for n in range(10):
                trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID, tool_call_id=f"c{n}")
                await self.settle()
            trace.on_session_end(session_id=SID, completed=True)
            await trace._drain()
        edits = [content for _, content in self.adapter.sent if "m.new_content" in content]
        self.assertEqual(len(edits), 3, "上限 3 次编辑，最后一次是结束")
        self.assertEqual(self.last()["org.fluxchat.agent"]["state"], "done")
        self.assertEqual(self.last()["org.fluxchat.agent"]["steps"], 10)

    async def test_the_list_keeps_the_latest_calls_and_short_titles(self):
        self.granted()
        with mock.patch.object(trace, "MAX_CALLS", 2):
            for n in range(3):
                trace.pre_tool_call(tool_name="x" * 300, args={}, session_id=SID, tool_call_id=f"c{n}")
            trace.on_session_end(session_id=SID, completed=True)
            await trace._drain()
        calls = self.last()["app.wonton.trace"]["calls"]
        self.assertEqual([call["id"] for call in calls], ["c1", "c2"])
        self.assertEqual(len(calls[0]["title"]), trace.MAX_TITLE)
        self.assertEqual(self.last()["org.fluxchat.agent"]["steps"], 3)

    async def test_a_broken_upstream_never_blocks_the_tool(self):
        # pre_tool_call 是上游的策略钩子：抛错就拦下工具。这里必须接住，返回 None。
        self.granted()

        def broken(*_args, **_kwargs):
            raise ImportError("上游改名了")

        with mock.patch.object(trace, "_upstream", broken):
            self.assertIsNone(trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID,
                                                  tool_call_id="c1"))
        trace.on_session_end(session_id=SID, completed=True)

    async def test_a_restart_turns_open_status_messages_uncertain(self):
        self.granted()
        trace.pre_tool_call(tool_name="read_file", args={}, session_id=SID, tool_call_id="c1")
        await self.settle()
        record = json.loads(Path(self.home.name, trace.OPEN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(list(record), ["$status1"])
        # 重启：进程没了（发送协程停掉、内存清空），网关第一次收到消息时收尾。
        for future in list(trace._S["inflight"]):
            future.cancel()
        await asyncio.sleep(0.05)
        trace._S.update({"runs": {}, "inflight": set(), "recovered": False})
        self.adapter.sent.clear()
        trace.pre_gateway_dispatch(event=None)
        await trace._drain()
        room, edit = self.adapter.sent[0]
        self.assertEqual(room, ROOM)
        self.assertEqual(edit["m.relates_to"], {"rel_type": "m.replace", "event_id": "$status1"})
        content = edit["m.new_content"]
        self.assertEqual(content["org.fluxchat.agent"]["state"], "failed")
        self.assertEqual(content["app.wonton.end"], "uncertain")
        self.assertEqual(content["app.wonton.trace"]["calls"][0]["id"], "c1")
        self.assertFalse(Path(self.home.name, trace.OPEN_FILE).exists())
        # 只收尾一次。
        trace.pre_gateway_dispatch(event=None)
        await trace._drain()
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_a_failed_send_only_logs(self):
        self.granted()
        self.adapter.fail = True
        with self.assertLogs(trace.logger, "WARNING"):
            trace.pre_tool_call(tool_name="terminal", args={}, session_id=SID, tool_call_id="c1")
            trace.on_session_end(session_id=SID, completed=True)
            await trace._drain()
        self.assertEqual(self.adapter.sent, [])


if __name__ == "__main__":
    unittest.main()
