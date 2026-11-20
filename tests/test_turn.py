"""每轮身份头、调用链和出箱：用假的 MCP 会话、网关和核心 HTTP 验证协议字段。"""

import asyncio
import contextlib
import contextvars
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
mautrix_errors = types.ModuleType("mautrix.errors")

class EventType:
    ROOM_MESSAGE = "m.room.message"

class MForbidden(Exception):
    pass

class MNotFound(Exception):
    pass

mautrix_types.EventType = EventType
mautrix_errors.MForbidden = MForbidden
mautrix_errors.MNotFound = MNotFound
mautrix.types = mautrix_types
mautrix.errors = mautrix_errors
sys.modules.setdefault("mautrix", mautrix)
sys.modules.setdefault("mautrix.types", mautrix_types)
sys.modules.setdefault("mautrix.errors", mautrix_errors)

delegation = types.ModuleType("agent.delegation_context")
_delegated = contextvars.ContextVar("delegated_child", default=False)

@contextlib.contextmanager
def _delegated_child(session_id=None):
    token = _delegated.set(True)
    try:
        yield
    finally:
        _delegated.reset(token)

delegation.delegated_child_context = _delegated_child
delegation.is_delegated_child_context = lambda: _delegated.get()
agent = types.ModuleType("agent")
agent.delegation_context = delegation
sys.modules.setdefault("agent", agent)
sys.modules.setdefault("agent.delegation_context", delegation)

executions = types.ModuleType("cron.execution_identity")
EXECUTION = contextvars.ContextVar("cron_execution", default=None)  # 上游 cron 执行中的那份标记
executions._CURRENT = EXECUTION
jobs_module = types.ModuleType("cron.jobs")
JOBS = {}
jobs_module.get_job = JOBS.get
cron = types.ModuleType("cron")
cron.jobs = jobs_module
cron.execution_identity = executions
sys.modules.setdefault("cron", cron)
sys.modules.setdefault("cron.jobs", jobs_module)
sys.modules.setdefault("cron.execution_identity", executions)

from wonton import turn  # noqa: E402

OWNER = "@owner:example.org"
ROOM = "!dm1:example.org"
SID = "session-1"
MOM = "@mom:example.org"
MOM_ROOM = "!dm2:example.org"
MOM_SID = "session-2"


class Origin:
    chat_id, thread_id, platform, user_id = ROOM, None, "matrix", OWNER


SOURCE = Origin


class Entry:
    def __init__(self):
        self.origin = Origin()


class MomOrigin:
    chat_id, thread_id, platform, user_id = MOM_ROOM, None, "matrix", MOM


class MomEntry:
    origin = MomOrigin()


class Store:
    def __init__(self):
        self.keys = {}
        self.entries = {SID: Entry(), MOM_SID: MomEntry()}

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


class MomSource:
    chat_id, platform, user_id, thread_id = MOM_ROOM, "matrix", MOM, None


class MomEvent:
    message_id = "$event-2"
    source = MomSource()


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
        self.vars = {}  # 会话环境的 ContextVar（和上游 gateway.session_context 一样按 context 走）
        self.sent = []
        self.answer = None
        turn._S.update({
            "loop": None, "runner": None, "session_store": None, "origins": {}, "pending": {},
            "active": {}, "reported": set(), "ending": set(), "worker": True, "unknown": set(),
            "warned": False, "ctx": None,
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
        env, vars = self.env, self.vars

        def get(name, default=""):
            var = vars.get(name)
            value = var.get() if var else ""
            return value if value else env.get(name, default)

        module = types.ModuleType("gateway.session_context")
        module.get_session_env = get
        return module

    def bind(self, name, value):
        """在当前的 context 里绑一项会话环境（上游由 Agent 线程上的 set_session_vars 做）。"""
        var = self.vars.setdefault(name, contextvars.ContextVar(name, default=""))
        var.set(value)

    async def _language(self, sender):
        return "zh-Hans"

    def _post(self, url, json=None, headers=None, **kwargs):
        self.sent.append((url, dict(json), headers))
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

    async def test_interleaved_turns_each_carry_their_own_identity(self):
        # 上游把 pre_llm_call 放在别的线程里、用拷贝的 context 跑，工具调用在 Agent 线程自己的 context 里。
        # 顺序 A 钩子、B 钩子、A 工具、B 工具，两边带的得各是各的。
        loop = asyncio.get_running_loop()
        a, b = contextvars.copy_context(), contextvars.copy_context()

        def hook(ctx, sid, sender):
            # 钩子的线程再拷一份：那里 set 的 contextvar 回不到 Agent 线程。
            contextvars.copy_context().run(turn.pre_llm_call, session_id=sid, sender_id=sender)

        async def call_in(ctx):
            task = ctx.run(loop.create_task, handlers._call_tool_racing_stdio_death(self.server, "x", {}))
            return await task

        turn.pre_gateway_dispatch(event=Event(), session_store=self.store)
        turn.pre_gateway_dispatch(event=MomEvent(), session_store=self.store)
        a.run(self.bind, "HERMES_SESSION_ID", SID)
        b.run(self.bind, "HERMES_SESSION_ID", MOM_SID)
        await loop.run_in_executor(None, hook, a, SID, OWNER)
        await loop.run_in_executor(None, hook, b, MOM_SID, MOM)
        await call_in(a)
        await call_in(b)
        owner_headers, mom_headers = self.server.session.headers[-2:]
        self.assertEqual(owner_headers["X-Wonton-On-Behalf-Of"], OWNER)
        self.assertEqual(owner_headers["wonton-session"], SID)
        self.assertEqual(mom_headers["X-Wonton-On-Behalf-Of"], MOM)
        self.assertEqual(mom_headers["wonton-session"], MOM_SID)

    async def test_a_second_session_does_not_take_over_the_firsts_identity(self):
        # 主人那一轮还开着，会话环境里的会话号也还指着它；妈妈那一轮这时开始，
        # 它的工具调用带的得是它自己。串号时卡片会挂到主人那条链上，停链撤不回。
        await self.turn()
        self.server.session.calls.clear()
        self.server.session.headers.clear()

        async def mom_turn():
            # 妈妈这一轮的 Agent 线程绑的是它自己的会话号，工具线程拷的就是这份。
            self.bind("HERMES_SESSION_ID", MOM_SID)
            turn.pre_llm_call(session_id=MOM_SID, sender_id=MOM)
            return await handlers._call_tool_racing_stdio_death(self.server, "x", {})

        self.assertEqual(await asyncio.create_task(mom_turn()), "ok")
        headers = self.server.session.headers[0]
        self.assertEqual(headers["X-Wonton-On-Behalf-Of"], MOM)
        self.assertEqual(headers["wonton-session"], MOM_SID)

    async def test_without_a_turn_a_lone_open_session_is_not_guessed(self):
        # 环境里的会话号是别人的一轮占着的、本会话又没记下来源时不猜：宁可不带，也不挂到别人的链上。
        await self.turn()
        self.server.session.calls.clear()
        self.server.session.headers.clear()
        self.env.clear()
        with self.assertLogs("wonton.turn", level="WARNING") as logs:
            await handlers._call_tool_racing_stdio_death(self.server, "x", {})
        self.assertNotIn("X-Wonton-On-Behalf-Of", self.server.session.headers[0])
        self.assertIn("身份取不到", "\n".join(logs.output))

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
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start", "start"])

    async def test_a_limit_refusal_blocks_the_turn_and_still_reports_the_end(self):
        self.answer = lambda body: Response({"type": "limit_reached", "kind": "depth"}, status=429)
        block, _ = await self.turn()
        self.assertEqual(block, {"action": "block", "message": turn.CHAIN_LIMIT["zh-Hans"]})
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start", "end"])

    async def test_every_refusal_has_its_own_words(self):
        for status, body, text in (
            (403, {"type": "permission_denied"}, turn.CHAIN_DENIED),
            (400, {"type": "invalid_input", "refusal": "self_call"}, turn.CHAIN_SELF),
            (400, {"type": "invalid_input", "refusal": "loop"}, turn.CHAIN_LOOP),
        ):
            self.answer = lambda _body, status=status, body=body: Response(body, status=status)
            turn._S.update({"reported": set(), "ending": set(), "origins": {}})
            self.sent.clear()
            block, _ = await self.turn()
            self.assertEqual(block["message"], text["zh-Hans"])

    async def test_an_unclear_refusal_carries_the_cores_reason(self):
        # 同样的 invalid_input 也可能是这一轮的会话还挂在别的链上，类别本身说不清。
        self.answer = lambda _body: Response(
            {"type": "invalid_input", "refusal": "other"}, 400, "会话 s-1 已登记在别的链上"
        )
        block, _ = await self.turn()
        self.assertEqual(
            block["message"], turn.CHAIN_OTHER["zh-Hans"] + "\n\n会话 s-1 已登记在别的链上"
        )

    async def test_an_unknown_refusal_type_isnt_called_a_limit(self):
        self.answer = lambda _body: Response({"type": "other"}, 400)
        block, _ = await self.turn()
        self.assertEqual(block["message"], turn.CHAIN_OTHER["zh-Hans"])

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

    async def test_the_fresh_message_wins_over_the_previous_origin(self):
        # 被打断、重置后 peek 不到会话 id，pre_gateway_dispatch 只写 pending；下一句仍要报新消息，
        # 不然核心按旧消息的唤醒标签找到停了的链，整轮被拒。
        await self.turn()
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.sent.clear()
        with mock.patch.object(Event, "message_id", "$event-2"):
            await self.turn()
        self.assertEqual(self.sent[0][1]["event_id"], "$event-2")

    async def test_a_stopped_chain_is_reported_again_without_the_message(self):
        def answer(body):
            if body["event_id"]:
                return Response({"type": "stopped"}, 409)
            return Response({"chain": "c-2", "layer": 1})

        self.answer = answer
        block, _ = await self.turn()
        self.assertIsNone(block)
        self.assertEqual([body["event_id"] for _, body, _ in self.sent], ["$event-1", ""])
        turn.on_session_end(session_id=SID)
        await asyncio.sleep(0.05)
        self.assertEqual([body["phase"] for _, body, _ in self.sent], ["start", "start", "end"])


class CronExecution:
    job_id = "job-1"


class CronCtx:
    """插件上下文中定时任务那一部分：正在执行的任务由它报出。"""

    execution = None

    def register_locale_dir(self, path):
        pass

    def register_hook(self, name, callback):
        pass

    def current_cron_execution(self):
        # 上游在委派出去的子代理里特意返回 None（cron/execution_identity.py）。
        return None if delegation.is_delegated_child_context() else self.execution


class CronCase(TurnCase):
    def setUp(self):
        super().setUp()
        self.ctx = CronCtx()
        JOBS.clear()
        JOBS["job-1"] = {"origin": {"platform": "matrix", "chat_id": MOM_ROOM, "user_id": MOM}}
        EXECUTION.set(None)

    async def cron_call(self):
        self.env["HERMES_SESSION_ID"] = "cron_job-1_20261120_090000"
        self.ctx.execution = CronExecution()
        EXECUTION.set(self.ctx.execution)
        turn.register(self.ctx)
        return await handlers._call_tool_racing_stdio_death(self.server, "x", {})

    async def test_a_cron_tool_call_speaks_for_the_job_creator(self):
        self.assertEqual(await self.cron_call(), "ok")
        headers = self.server.session.headers[-1]
        self.assertEqual(headers["X-Wonton-On-Behalf-Of"], MOM)
        self.assertNotIn("wonton-session", headers)
        self.assertEqual(headers["Mcp-Param-Owner"], "1")

    async def test_a_cron_job_from_another_platform_carries_no_identity(self):
        JOBS["job-1"] = {"origin": {"platform": "telegram", "chat_id": "42", "user_id": MOM}}
        await self.cron_call()
        headers = self.server.session.headers[-1]
        self.assertNotIn("X-Wonton-On-Behalf-Of", headers)
        self.assertNotIn("wonton-session", headers)

    async def test_a_cron_job_with_an_odd_platform_value_carries_no_identity(self):
        JOBS["job-1"] = {"origin": {"platform": "Matrix", "chat_id": MOM_ROOM, "user_id": MOM}}
        await self.cron_call()
        self.assertNotIn("X-Wonton-On-Behalf-Of", self.server.session.headers[-1])

    async def test_a_cron_job_without_a_creator_carries_no_identity(self):
        JOBS["job-1"] = {"origin": {"platform": "matrix", "chat_id": MOM_ROOM}}
        await self.cron_call()
        self.assertNotIn("X-Wonton-On-Behalf-Of", self.server.session.headers[-1])

    async def test_a_cron_job_gone_from_the_store_carries_no_identity(self):
        JOBS.clear()
        await self.cron_call()
        self.assertNotIn("X-Wonton-On-Behalf-Of", self.server.session.headers[-1])

    async def test_a_delegated_child_of_a_cron_run_speaks_for_the_creator(self):
        # 子代理跑在拷来的 context 里，上游的 current_cron_execution() 对它返回 None；执行标记本身还在。
        self.ctx.execution = CronExecution()
        EXECUTION.set(self.ctx.execution)
        turn.register(self.ctx)

        async def child_call():
            self.bind("HERMES_SESSION_ID", "subagent-1")
            with delegation.delegated_child_context("subagent-1"):
                self.assertIsNone(self.ctx.current_cron_execution())
                return await handlers._call_tool_racing_stdio_death(self.server, "x", {})

        task = contextvars.copy_context().run(asyncio.create_task, child_call())
        self.assertEqual(await task, "ok")
        headers = self.server.session.headers[-1]
        self.assertEqual(headers["X-Wonton-On-Behalf-Of"], MOM)
        self.assertNotIn("wonton-session", headers)

    async def test_a_cron_run_installs_the_headers_without_a_gateway_message(self):
        # 进程中只跑定时任务、没收到过网关消息时，pre_gateway_dispatch 不会来，工具调用靠 pre_llm_call 包。
        handlers._call_tool_racing_stdio_death = _upstream
        self.assertFalse(getattr(handlers._call_tool_racing_stdio_death, "_wonton_turn", False))
        turn.pre_llm_call(session_id="cron_job-1_20261120_090000")
        self.assertTrue(getattr(handlers._call_tool_racing_stdio_death, "_wonton_turn", False))


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        turn._S.update({"origins": {SID: {"session_key": "key:!dm1:example.org", "source": SOURCE}}, "runner": None})
        self.receipts = []

    async def test_cancel_turn_goes_the_same_way_as_stop(self):
        runner = mock.Mock()
        runner._interrupt_and_clear_session = mock.AsyncMock()
        turn._S["runner"] = runner
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-1", "kind": {"type": "cancel_turn", "session": SID}})
        source = turn._S["origins"][SID]["source"]
        runner._interrupt_and_clear_session.assert_awaited_once_with(
            "key:!dm1:example.org", source,
            interrupt_reason="云吞停链", invalidation_reason="wonton_chain_stopped")
        runner._interrupt_running_turn.assert_not_called()
        self.assertEqual(self.receipts, [("o-1", {"result": "sent", "event_id": ""})])

    async def test_cancel_without_the_source_frees_the_slot_itself(self):
        runner = mock.Mock()
        runner._interrupt_running_turn.return_value = 7
        turn._S.update({"runner": runner, "origins": {SID: {"session_key": "key:!dm1:example.org"}}})
        with mock.patch.object(turn, "_receipt", self._receipt):
            await turn._do_outbox({"id": "o-2", "kind": {"type": "cancel_turn", "session": SID}})
        runner._drop_turn_slot.assert_called_once_with("key:!dm1:example.org", run_generation=7)
        self.assertEqual(self.receipts, [("o-2", {"result": "sent", "event_id": ""})])

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
