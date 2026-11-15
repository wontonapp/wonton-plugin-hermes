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
        return "ok"


class Server:
    def __init__(self):
        self.session = Session()


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class TurnCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store()
        self.server = Server()
        turn._S.update({"loop": None, "runner": None, "session_store": None, "origins": {}, "tokens": {}, "worker": True})
        turn._install_headers()
        self.vars = mock.patch.dict("os.environ", {
            "WONTON_MCP_URL": "http://core:8080/mcp/bus-token",
            "MATRIX_ACCESS_TOKEN": "bot-token",
        })
        self.vars.start()
        self.addCleanup(self.vars.stop)
        self.chain = mock.patch.object(turn, "_post_chain", lambda *a, **k: None)
        self.chain.start()
        self.addCleanup(self.chain.stop)

    async def turn(self):
        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        turn.pre_llm_call(session_id=SID, sender_id=OWNER)
        return await handlers._call_tool_racing_stdio_death(
            self.server, "wonton__save_file", {"x": 1})

    async def test_tool_call_carries_sender_session_and_origin(self):
        self.assertEqual(await self.turn(), "ok")
        name, args, kwargs = self.server.session.calls[0]
        self.assertEqual((name, args), ("wonton__save_file", {"x": 1}))
        headers = self.server.session._resolve_param_headers(name, args)
        self.assertEqual(headers["X-Wonton-On-Behalf-Of"], OWNER)
        self.assertEqual(headers["wonton-session"], SID)
        self.assertEqual(headers["Mcp-Param-Owner"], "1")
        self.assertEqual(kwargs["meta"], {"app.wonton/origin": {"room_id": ROOM}})

    async def test_tool_call_without_turn_has_no_headers(self):
        self.server.session._wonton_turn = False
        await handlers._call_tool_racing_stdio_death(
            self.server, "x", {})
        self.assertEqual(self.server.session._resolve_param_headers("x", {}), {"Mcp-Param-Owner": "1"})
        self.assertNotIn("meta", self.server.session.calls[0][2])

    async def test_end_clears_the_turn(self):
        await self.turn()
        turn.on_session_end(session_id=SID)
        await handlers._call_tool_racing_stdio_death(
            self.server, "x", {})
        self.assertNotIn("wonton-session", self.server.session._resolve_param_headers("x", {}))

    async def test_chain_start_and_end_are_reported(self):
        sent = []

        def post(url, json=None, headers=None, **kwargs):
            sent.append((url, json, headers))
            return Response({"chain": "c-1", "layer": 1} if json["phase"] == "start" else {})

        self.chain.stop()
        with mock.patch.object(turn.httpx, "post", post):
            await self.turn()
            turn.on_session_end(session_id=SID)
            await asyncio.sleep(0.05)
        self.assertEqual(sent[0][0], "http://core:8080/mcp/bus-token/agent/chains")
        self.assertEqual(sent[0][1], {
            "phase": "start", "session": SID, "room": ROOM, "sender": OWNER, "event_id": "$event-1",
        })
        self.assertEqual(sent[0][2], {"X-Wonton-Bot-Token": "bot-token"})
        self.assertEqual(sent[1][1], {"phase": "end", "session": SID})


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        turn._S.update({"origins": {SID: {"session_key": "key:dm"}}, "runner": None})
        self.receipts = []

    async def test_cancel_turn_interrupts_the_session(self):
        runner = mock.Mock()
        turn._S["runner"] = runner
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-1", "kind": {"type": "cancel_turn", "session": SID}})
        runner._interrupt_running_turn.assert_called_once()
        self.assertEqual(self.receipts, [("o-1", {"result": "sent", "event_id": ""})])

    async def test_message_goes_to_the_joined_dm_with_item_id(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {"@mom:example.org": [ROOM]}
        client.get_joined_rooms.return_value = [ROOM]
        client.send_message_event.return_value = "$sent"
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org"}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-2", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-2", {"result": "sent", "event_id": "$sent"})])
        self.assertEqual(client.send_message_event.await_args.kwargs["txn_id"], "o-2")

    async def test_message_without_a_joined_dm_fails(self):
        client = mock.AsyncMock()
        client.get_account_data.return_value = {}
        client.get_joined_rooms.return_value = []
        with mock.patch.object(turn, "_client", lambda: client), \
                mock.patch.dict("os.environ", {"MATRIX_USER_ID": "@bot:example.org"}), \
                mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-3", "kind": {"type": "message", "recipient": "mom", "body": "hi"}})
        self.assertEqual(self.receipts, [("o-3", {"result": "failed", "category": "no_room"})])

    async def _receipt(self, item_id, result):
        self.receipts.append((item_id, result))


if __name__ == "__main__":
    unittest.main()
