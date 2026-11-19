"""每轮身份头、调用链和出箱：用假的 MCP 会话、网关和核心 HTTP 验证协议字段。"""

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

handlers = types.ModuleType("tools.mcp_tool_handlers")

async def _upstream(server, tool_name, args, progress_callback=None):
    return await server.session.call_tool(tool_name, arguments=args, progress_callback=progress_callback)

handlers._call_tool_racing_stdio_death = _upstream
tools = types.ModuleType("tools")
tools.mcp_tool_handlers = handlers
sys.modules.setdefault("tools", tools)
sys.modules.setdefault("tools.mcp_tool_handlers", handlers)

mautrix = types.ModuleType("mautrix")
mautrix_types = types.ModuleType("mautrix.types")

class EventType:
    ROOM_MESSAGE = "m.room.message"

mautrix_types.EventType = EventType
mautrix.types = mautrix_types
sys.modules.setdefault("mautrix", mautrix)
sys.modules.setdefault("mautrix.types", mautrix_types)

from wonton import turn  # noqa: E402

OWNER = "@owner:example.org"
ROOM = "!dm1:example.org"
SID = "session-1"


class Origin:
    chat_id, thread_id, platform, user_id = ROOM, None, "matrix", OWNER


class Entry:
    def __init__(self):
        self.origin = Origin()


class Store:
    def __init__(self):
        self.keys = {}
        self.entries = {SID: Entry()}

    def _generate_session_key(self, source):
        return f"key:{source.chat_id}"

    def peek_session_id(self, key):
        return self.keys.get(key)

    def lookup_by_session_id(self, sid):
        return self.entries.get(sid)


class Source:
    chat_id, platform, user_id, thread_id = ROOM, "matrix", OWNER, None


class Event:
    message_id = "$event-1"
    source = Source()


class Session:
    def __init__(self):
        self.headers = []
        self.calls = []

    def _resolve_param_headers(self, name, arguments):
        return {"Mcp-Param-Owner": "1"}

    async def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments, kwargs))
        self.headers.append(self._resolve_param_headers(name, arguments))
        return "ok"


class Server:
    def __init__(self):
        self.session = Session()


class Response:
    def __init__(self, body=None, status=200, detail=""):
        self.body, self.status_code = body, status
        self.headers = {"X-Wonton-Detail": detail}
        self.is_success = status < 400

    def raise_for_status(self):
        if not self.is_success:
            raise turn.httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self.body


class TurnCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store()
        self.server = Server()
        self.env = {"HERMES_SESSION_ID": SID}
        self.sent = []
        self.answer = None
        turn._S.update({
            "loop": None, "runner": None, "session_store": None, "origins": {}, "pending": {},
            "active": {}, "reported": set(), "ending": set(), "worker": True,
        })
        turn._install_headers()
        modules = mock.patch.dict("os.environ", {
            "WONTON_MCP_URL": "http://core:8080/mcp/bus-token",
            "MATRIX_ACCESS_TOKEN": "bot-token",
        })
        modules.start()
        self.addCleanup(modules.stop)
        context = mock.patch.dict(sys.modules, {"gateway.session_context": self._context()})
        context.start()
        self.addCleanup(context.stop)
        lang = mock.patch.object(turn, "language", self._language)
        lang.start()
        self.addCleanup(lang.stop)
        http = mock.patch.object(turn.httpx, "post", self._post)
        http.start()
        self.addCleanup(http.stop)

    def _context(self):
        env = self.env
        module = types.ModuleType("gateway.session_context")
        module.get_session_env = lambda name, default="": env.get(name, default)
        return module

    async def _language(self, sender):
        return "zh-Hans"

    def _post(self, url, json=None, headers=None, **kwargs):
        self.sent.append((url, json, headers))
        if self.answer is not None:
            return self.answer(json)
        return Response({"chain": "c-1", "layer": 1}) if json["phase"] == "start" else Response({})

    async def turn(self):
        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        return turn.pre_llm_call(session_id=SID, sender_id=OWNER), await handlers._call_tool_racing_stdio_death(
            self.server, "wonton__save_file", {"x": 1})

    async def test_tool_call_carries_sender_session_and_origin(self):
        self.assertEqual((await self.turn())[1], "ok")
        name, args, kwargs = self.server.session.calls[0]
        self.assertEqual((name, args), ("wonton__save_file", {"x": 1}))
        headers = self.server.session.headers[0]
        self.assertEqual(headers["X-Wonton-On-Behalf-Of"], OWNER)
        self.assertEqual(headers["wonton-session"], SID)
        self.assertEqual(headers["Mcp-Param-Owner"], "1")
        self.assertEqual(kwargs["meta"], {"app.wonton/origin": {"room_id": ROOM}})

    async def test_tool_call_without_turn_has_no_headers(self):
        self.server.session._wonton_turn = False
        await handlers._call_tool_racing_stdio_death(
            self.server, "x", {})
        self.assertEqual(self.server.session.headers[-1], {"Mcp-Param-Owner": "1"})
        self.assertNotIn("meta", self.server.session.calls[0][2])

    async def test_the_identity_comes_from_the_session_not_the_hook_thread(self):
        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        self.env.pop("HERMES_SESSION_ID")
        turn.pre_llm_call(session_id=SID, sender_id=OWNER)
        self.env["HERMES_SESSION_ID"] = SID
        await handlers._call_tool_racing_stdio_death(self.server, "x", {})
        self.assertEqual(self.server.session.headers[-1]["X-Wonton-On-Behalf-Of"], OWNER)

    async def test_end_clears_the_turn(self):
        await self.turn()
        turn._S["pending"]["key:!dm1:example.org"] = {"sender": OWNER}
        turn.on_session_end(session_id=SID)
        self.assertNotIn("key:!dm1:example.org", turn._S["pending"])
        await handlers._call_tool_racing_stdio_death(
            self.server, "x", {})
        self.assertNotIn("wonton-session", self.server.session.headers[-1])

    async def test_end_without_a_start_is_not_reported(self):
        self.sent.clear()
        turn.on_session_end(session_id="never-started")
        await asyncio.sleep(0.05)
        self.assertEqual(self.sent, [])

    async def test_the_outbox_worker_task_is_retained(self):
        async def sleeper():
            await asyncio.sleep(60)

        turn._S.update({"loop": asyncio.get_running_loop(), "worker": None})
        with mock.patch.object(turn, "_outbox_loop", sleeper):
            turn._start_worker()
            self.assertIsInstance(turn._S["worker"], asyncio.Task)
            turn._S["worker"].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn._S["worker"]

    async def test_chain_start_and_end_are_reported(self):
        await self.turn()
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.assertEqual(self.sent[0][0], "http://core:8080/agent/chains/bus-token")
        self.assertEqual(self.sent[0][1], {
            "phase": "start", "session": SID, "room": ROOM, "sender": OWNER, "event_id": "$event-1",
        })
        self.assertEqual(self.sent[0][2], {"X-Wonton-Bot-Token": "bot-token"})
        self.assertEqual(self.sent[1][1], {"phase": "end", "session": SID})

    async def test_a_stopped_chain_blocks_the_turn_without_an_end(self):
        self.answer = lambda body: Response({"type": "stopped"}, status=409)
        block, _ = await self.turn()
        self.assertEqual(block, {"action": "block", "message": turn.CHAIN_STOPPED["zh-Hans"]})
        self.assertNotIn(SID, turn._S["active"])
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start"])

    async def test_a_limit_refusal_blocks_the_turn_and_still_reports_the_end(self):
        self.answer = lambda body: Response({"type": "limit_reached", "kind": "depth"}, status=429)
        block, _ = await self.turn()
        self.assertEqual(block, {"action": "block", "message": turn.CHAIN_LIMIT["zh-Hans"]})
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start", "end"])

    async def test_permission_and_busy_refusals_have_their_own_words(self):
        for status, body, text in (
            (403, {"type": "permission_denied"}, turn.CHAIN_DENIED),
            (429, {"type": "busy"}, turn.CHAIN_BUSY),
            (400, {"type": "invalid_input", "refusal": "self_call"}, turn.CHAIN_SELF),
            (400, {"type": "invalid_input", "refusal": "loop"}, turn.CHAIN_LOOP),
        ):
            self.answer = lambda _body, status=status, body=body: Response(body, status=status)
            turn._S.update({"reported": set(), "ending": set(), "origins": {}})
            self.sent.clear()
            block, _ = await self.turn()
            self.assertEqual(block["message"], text["zh-Hans"])

    async def test_an_unreported_chain_is_retried_on_the_next_call(self):
        self.answer = lambda body: Response({}, status=503)
        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        self.assertIsNone(turn.pre_llm_call(session_id=SID, sender_id=OWNER))
        self.sent.clear()
        self.answer = None
        self.assertIsNone(turn.pre_llm_call(session_id=SID, sender_id=OWNER))
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start"])
        self.assertIn(SID, turn._S["active"])

    async def test_the_start_is_reported_once_per_turn(self):
        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        turn.pre_llm_call(session_id=SID, sender_id=OWNER)
        turn.pre_llm_call(session_id=SID, sender_id=OWNER)
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start"])

    async def test_a_new_message_reports_the_chain_again(self):
        await self.turn()
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.sent.clear()
        self.store.keys["key:!dm1:example.org"] = SID
        await self.turn()
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start"])


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        turn._S.update({"origins": {SID: {"session_key": "key:!dm1:example.org"}}, "runner": None})
        self.receipts = []

    async def test_cancel_turn_interrupts_the_session(self):
        runner = mock.Mock()
        turn._S["runner"] = runner
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-1", "kind": {"type": "cancel_turn", "session": SID}})
        runner._interrupt_running_turn.assert_called_once()
        self.assertEqual(self.receipts, [("o-1", {"result": "sent", "event_id": ""})])

    async def test_cancel_without_an_in_memory_origin_uses_the_session_store(self):
        runner = mock.Mock()
        turn._S.update({"runner": runner, "origins": {}, "session_store": Store()})
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-9", "kind": {"type": "cancel_turn", "session": SID}})
        runner._interrupt_running_turn.assert_called_once()
        self.assertEqual(self.receipts, [("o-9", {"result": "sent", "event_id": ""})])

    async def test_cancel_unknown_session_fails(self):
        turn._S["runner"] = mock.Mock()
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-0", "kind": {"type": "cancel_turn", "session": "gone"}})
        self.assertEqual(self.receipts, [("o-0", {"result": "failed", "category": "other"})])

    async def test_message_goes_to_the_joined_dm_with_item_id(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {"@mom:example.org": [ROOM]}
        client.get_joined_rooms.return_value = [ROOM]
        client.send_message_event.return_value = "$sent"
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org", "MATRIX_HOMESERVER": ""}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-2", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-2", {"result": "sent", "event_id": "$sent"})])
        self.assertEqual(client.send_message_event.await_args.kwargs["txn_id"], "o-2")

    async def test_message_without_a_joined_dm_fails(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {}
        client.get_joined_rooms.return_value = []
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org", "MATRIX_HOMESERVER": ""}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-3", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-3", {"result": "failed", "category": "no_room"})])

    async def test_message_uses_the_homeserver_host(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {"@mom:https.example.org": [ROOM]}
        client.get_joined_rooms.return_value = [ROOM]
        client.send_message_event.return_value = "$sent"
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {
                    "MATRIX_USER_ID": "@bot:wrong.example.org",
                    "MATRIX_HOMESERVER": "https://https.example.org/",
                }), mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-4", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-4", {"result": "sent", "event_id": "$sent"})])

    async def test_receipt_failure_does_not_kill_the_loop(self):
        polls = 0

        async def poll(client=None):
            nonlocal polls
            polls += 1
            if polls == 1:
                return {"id": "o-7", "kind": {"type": "bogus"}}
            await asyncio.sleep(0.01)
            return None

        async def boom(*args, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(turn, "_poll_outbox", poll), \
                mock.patch.object(turn, "_do_outbox", boom), \
                mock.patch.object(turn, "_receipt", boom):
            task = asyncio.create_task(turn._outbox_loop())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_send_timeout_is_other(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {"@mom:example.org": [ROOM]}
        client.get_joined_rooms.return_value = [ROOM]
        client.send_message_event.side_effect = asyncio.TimeoutError()
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org", "MATRIX_HOMESERVER": ""}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-6", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-6", {"result": "failed", "category": "other"})])

    async def test_account_data_error_is_not_an_empty_dm(self):
        client = mock.AsyncMock()
        client.get_account_data.side_effect = RuntimeError("boom")
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org", "MATRIX_HOMESERVER": ""}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-5", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-5", {"result": "failed", "category": "other"})])

    async def _receipt(self, item_id, result, client=None):
        self.receipts.append((item_id, result))


if __name__ == "__main__":
    unittest.main()
