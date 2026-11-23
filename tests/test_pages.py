"""网页连接带着打开者：只有网关写过头的连接换身份，其余照旧；这一轮认最后提交的连接。

上游替身见 ``tui_fake``，真形状在 ``tests/hermes`` 里跑。
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tui_fake  # noqa: E402
from wonton import pages, turn  # noqa: E402

ORIGINAL_INIT = tui_fake.WSTransport.__init__
MOM = "@mom:example.org"
ROOM = "!room:example.org"


class Store:
    """网关的会话表：派发时按来源算键、拿键换成已有的会话号。"""

    def __init__(self):
        self.keys = {}
        self.entries = {}

    def _generate_session_key(self, source, key_source=None):
        return f"key:{source.chat_id}"

    def peek_session_id(self, session_key):
        return self.keys.get(session_key)

    def lookup_by_session_id(self, session_id):
        return self.entries.get(session_id)


class Source:
    chat_id, platform, user_id, thread_id = ROOM, "matrix", MOM, None


class DadSource:
    chat_id, platform, user_id, thread_id = ROOM, "matrix", "@dad:example.org", None


class Event:
    message_id = "$event-1"
    source = Source()


class DadEvent:
    message_id = "$event-2"
    source = DadSource()


class Origin:
    chat_id, platform, user_id, thread_id = ROOM, "matrix", MOM, None


class Entry:
    """盘上的会话表条目：页面轮的结束把内存记录清掉以后，`_origin` 的兜底还得认回会话的来源。"""

    origin = Origin()


SID = "web-session"
OWNER = "@owner:example.org"
GATEWAY = {"user_id": "cloud-gateway", "provider": "local", "profile_id": "p1"}


def reset():
    turn._S.update({"loop": None, "worker": True, "webs": {}, "gateway": None, "server": None, "session_store": None, "page_warned": set(), "dispatched": {},
                    "warned": False, "reported": set(), "active": {}, "ending": set(), "unknown": set(),
                    "origins": {}, "pending": {}})
    upstream = tui_fake.install(turn)
    turn._S["server"] = upstream.server
    return upstream


def connect(opener=None, sid=SID, **kwargs):
    upstream = tui_fake.modules()
    transport = upstream.ws.WSTransport(tui_fake.WS(opener), None, auth_identity=dict(GATEWAY), **kwargs)
    upstream.server._rebind_live_transport(sid, tui_fake.session(sid), transport)
    return transport


def isolate_home(case):
    """每个用例一个空的 ``HERMES_HOME``：记录落在那儿，不串。"""
    home = tempfile.TemporaryDirectory()
    case.addCleanup(home.cleanup)
    env = mock.patch.dict(os.environ, {"HERMES_HOME": home.name})
    env.start()
    case.addCleanup(env.stop)
    return Path(home.name)


class WebIdentityCase(unittest.TestCase):
    def setUp(self):
        self.upstream = reset()
        isolate_home(self)

    def test_the_opener_becomes_the_identity_and_the_rest_stays(self):
        transport = connect(MOM)
        self.assertEqual(transport.auth_identity["user_id"], MOM)
        self.assertEqual(transport.auth_identity["provider"], "wonton")
        self.assertEqual(transport.auth_identity["profile_id"], "p1")
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_a_connection_without_the_header_keeps_the_gateway_identity(self):
        transport = connect()
        self.assertEqual(transport.auth_identity, GATEWAY)
        self.assertIsNone(turn._page_sender(SID))

    def test_the_websocket_without_headers_does_not_break_the_connection(self):
        upstream = tui_fake.modules()

        class Bare:
            pass

        transport = upstream.ws.WSTransport(Bare(), None, auth_identity=dict(GATEWAY))
        self.assertEqual(transport.auth_identity, GATEWAY)

    def test_the_last_person_to_send_speaks_for_that_turn(self):
        mom = connect(MOM)
        owner = connect(OWNER)
        self.assertEqual(turn._page_sender(SID), OWNER)
        connect(MOM)
        self.assertEqual(turn._page_sender(SID), MOM)
        self.assertEqual(mom.auth_identity["user_id"], MOM)

    def test_a_page_that_closed_still_speaks_for_its_session(self):
        mom = connect(MOM)
        mom.close()
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_a_closed_page_hands_over_to_the_one_still_open(self):
        mom = connect(MOM)
        connect(OWNER)
        mom.close()
        self.assertEqual(turn._page_sender(SID), OWNER)

    def test_a_terminal_after_the_page_still_reads_the_page(self):
        connect(MOM)
        self.upstream.server._rebind_live_transport(SID, tui_fake.session(SID), tui_fake.Transport("tui"))
        self.assertEqual(turn._page_sender(SID), MOM)
        connect(OWNER)
        self.assertEqual(turn._page_sender(SID), OWNER)

    def test_the_record_that_is_no_longer_attached_does_not_speak(self):
        mom = connect(MOM)
        connect(OWNER)
        # A 最后提交过（盘上和进程里都记着它），随后 A 的页签关了；上游 Fanout.close() 只把子连接摘掉，
        # 不置子连接的 _closed，记着的那对象仍看着“活着”。B 还开着，这一轮该 B 说。
        pages.remember_user(SID, MOM)
        turn._S["webs"][SID] = mom
        tui_fake.session(SID)["transport"].detach(mom)
        self.assertEqual(turn._page_sender(SID), OWNER)

    def test_one_session_ending_does_not_warn_for_another(self):
        turn._S["server"] = object()  # 没有 _sessions
        turn._page_sender(SID)
        with self.assertLogs("wonton.turn", "WARNING"):
            turn._page_sender("other")

    def test_no_upstream_session_table_is_a_debug_round_not_the_one_shot(self):
        turn._S["server"] = None
        with self.assertLogs("wonton.turn", "DEBUG") as logs:
            self.assertIsNone(turn._page_sender(SID))
            self.assertIsNone(turn._page_sender(SID))
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(turn._S["page_warned"], set())

    def test_a_changed_upstream_shape_warns_once_per_session(self):
        turn._S["server"] = object()  # 没有 _sessions
        with self.assertLogs("wonton.turn", "WARNING") as logs:
            self.assertIsNone(turn._page_sender(SID))
            self.assertIsNone(turn._page_sender(SID))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(turn._S["page_warned"], {SID})
        turn.on_session_end(session_id=SID)
        with self.assertLogs("wonton.turn", "WARNING"):
            turn._page_sender(SID)

    def test_a_server_internal_connection_is_not_a_person(self):
        upstream = tui_fake.modules()
        transport = upstream.ws.WSTransport(
            tui_fake.WS(), None,
            auth_identity={"user_id": "server-internal", "provider": "server-internal"})
        upstream.server._rebind_live_transport(SID, tui_fake.session(SID), transport)
        self.assertEqual(transport.auth_identity["provider"], "server-internal")
        self.assertIsNone(turn._page_sender(SID))

    def test_a_page_without_an_opener_does_not_drop_the_open_one(self):
        connect(MOM)
        connect()  # 旧版页面，头还没写进来
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_a_closed_terminal_hands_back_to_a_page_still_open(self):
        terminal = tui_fake.Transport("tui")
        self.upstream.server._rebind_live_transport(SID, tui_fake.session(SID), terminal)
        connect(OWNER)
        terminal._closed = True
        self.assertEqual(turn._page_sender(SID), OWNER)

    def test_another_session_does_not_use_this_one(self):
        connect(MOM)
        self.assertIsNone(turn._page_sender("other"))

    def test_end_forgets_the_page_and_a_closed_one_stays_forgotten(self):
        mom = connect(MOM)
        turn.on_session_end(session_id=SID)
        self.assertEqual(turn._S["webs"], {})
        mom.close()
        self.assertIsNone(turn._page_sender(SID))

    def test_the_turn_carries_the_opener_and_the_tools_too(self):
        connect(MOM)
        with mock.patch.object(turn, "_chain_start", lambda body: ("grant", {}, "")):
            turn.pre_llm_call(session_id=SID, sender_id="uid:1000")
        sender, kind = turn._identity(SID)
        self.assertEqual(sender["sender"], MOM)
        self.assertIsNone(sender["room"])
        self.assertEqual(kind, "会话环境")

    def test_an_im_message_on_the_open_session_keeps_its_room(self):
        connect(OWNER)  # 网页把这条房间会话打开，盘上记着开页的人
        store = Store()
        store.keys[f"key:{ROOM}"] = SID
        turn.pre_gateway_dispatch(event=Event(), session_store=store)
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id=MOM)
        self.assertEqual(bodies[0]["sender"], MOM)
        self.assertEqual(bodies[0]["room"], ROOM)
        self.assertEqual(bodies[0]["event_id"], "$event-1")
        sender, _ = turn._identity(SID)
        self.assertEqual(sender["sender"], MOM)
        self.assertEqual(sender["room"], ROOM)

    def test_the_next_page_turn_goes_back_to_the_opener(self):
        connect(OWNER)
        store = Store()
        store.keys[f"key:{ROOM}"] = SID
        turn.pre_gateway_dispatch(event=Event(), session_store=store)
        with mock.patch.object(turn, "_chain_start", lambda body: ("grant", "", "")):
            turn.pre_llm_call(session_id=SID, sender_id=MOM)
        turn.on_session_end(session_id=SID)  # 这一轮结束了
        connect(OWNER)  # 这一页又敲了一句
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id="uid:1000")
        self.assertEqual(bodies[0]["sender"], OWNER)

    def test_a_page_turn_does_not_eat_the_im_dispatch_of_the_next_message(self):
        connect(OWNER)
        store = Store()
        store.keys[f"key:{ROOM}"] = SID
        store.entries[SID] = Entry()
        turn.pre_gateway_dispatch(event=Event(), session_store=store)  # IM 的消息先进来了，网页轮先跑
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id="uid:1000")
        turn.on_session_end(session_id=SID)  # 网页轮先结束，标记要留给还没跑的 IM 轮
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id=MOM)
        self.assertEqual(bodies[0]["sender"], OWNER)
        self.assertEqual(bodies[0]["room"], "")
        self.assertEqual(bodies[1]["sender"], MOM)
        self.assertEqual(bodies[1]["room"], ROOM)
        sender, _ = turn._identity(SID)
        self.assertEqual(sender["sender"], MOM)
        self.assertEqual(sender["room"], ROOM)

    def test_two_im_senders_do_not_take_each_others_dispatch(self):
        connect(OWNER)  # 页面开着这条房间会话：认错的那一轮会退回开页的人
        store = Store()
        store.keys[f"key:{ROOM}"] = SID
        turn.pre_gateway_dispatch(event=Event(), session_store=store)
        turn.pre_gateway_dispatch(event=DadEvent(), session_store=store)  # 后一条把 origins 盖掉
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id=MOM)
            turn.pre_llm_call(session_id=SID, sender_id=DadSource.user_id)
        self.assertEqual(bodies[0]["sender"], MOM)
        self.assertEqual(bodies[0]["room"], ROOM)
        self.assertEqual(bodies[0]["event_id"], "$event-1")
        self.assertEqual(bodies[1]["sender"], DadSource.user_id)
        self.assertEqual(bodies[1]["room"], ROOM)
        self.assertEqual(bodies[1]["event_id"], "$event-2")
        self.assertEqual(turn._S["dispatched"], {})

    def test_a_terminal_tab_turn_does_not_take_the_im_dispatch(self):
        connect(OWNER)
        store = Store()
        store.keys[f"key:{ROOM}"] = SID
        turn.pre_gateway_dispatch(event=Event(), session_store=store)  # IM 先进来，页签的轮先跑
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id="uid:1000")
            turn.pre_llm_call(session_id=SID, sender_id="")  # 认不出设备的连接
        turn.on_session_end(session_id=SID)  # 页签的轮先结束，标记要留给还没跑的 IM 轮
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id=MOM)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["sender"], OWNER)
        self.assertEqual(bodies[0]["room"], "")
        self.assertEqual(bodies[1]["sender"], MOM)
        self.assertEqual(bodies[1]["room"], ROOM)
        self.assertEqual(bodies[1]["event_id"], "$event-1")


    def test_a_page_turn_starts_the_chain_without_a_room(self):
        connect(MOM)
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id="uid:1000")
        self.assertEqual(bodies[0]["sender"], MOM)
        self.assertEqual(bodies[0]["room"], "")

    def test_install_is_idempotent(self):
        init = self.upstream.ws.WSTransport.__init__
        self.assertTrue(pages.install(turn))
        self.assertIs(self.upstream.ws.WSTransport.__init__, init)
        connect(MOM)
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_install_without_the_upstream_says_so(self):
        ws = sys.modules.pop("tui_gateway.ws")
        sys.modules["tui_gateway.ws"] = None  # import 时抛 ImportError
        try:
            with self.assertLogs("wonton.pages", "ERROR"):
                self.assertFalse(pages.install(turn))
        finally:
            sys.modules.pop("tui_gateway.ws", None)
            sys.modules["tui_gateway.ws"] = ws


HERE = Path(__file__).resolve().parent


class PageFilesCase(unittest.TestCase):
    """控制台和网关不是一个进程，打开者只能落在 ``HERMES_HOME`` 下的文件里交接。"""

    def setUp(self):
        self.upstream = reset()
        self.home = isolate_home(self)

    def note(self, opener, sid=SID):
        opener_for = self.upstream.ws.WSTransport(tui_fake.WS(opener), None, auth_identity=dict(GATEWAY))
        return opener_for

    def test_a_frame_carries_the_session_the_page_serves(self):
        page = self.note(MOM)
        page.write({"method": "event", "params": {"type": "session.info", "session_id": SID}})
        self.assertEqual(pages.recorded(SID), MOM)
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_the_pty_connection_records_the_page_on_the_active_session(self):
        path = self.home / "active.json"
        session = tui_fake.PTY(SID, path)
        session.input.append(b"x")
        asyncio.run(tui_fake.chat()._pump_keepalive_input(tui_fake.WS(MOM), session))
        self.assertEqual(session.written, [b"x"])
        self.assertEqual(pages.recorded(SID), MOM)

    def test_a_reconnected_terminal_keeps_one_layer(self):
        path = self.home / "active.json"
        session = tui_fake.PTY(SID, path)
        session.input.append(b"a")
        asyncio.run(tui_fake.chat()._pump_keepalive_input(tui_fake.WS(MOM), session))
        session.input.append(b"b")
        with mock.patch.object(pages, "_note_marker", wraps=pages._note_marker) as note:
            asyncio.run(tui_fake.chat()._pump_keepalive_input(tui_fake.WS(MOM), session))
        self.assertEqual(note.call_count, 2)  # 连上时一次、这一键一次；再包一层会是 3

    def test_one_record_per_connection_and_session(self):
        path = self.home / "active.json"
        session = tui_fake.PTY(SID, path)
        session.input.extend([b"a", b"b"])
        with mock.patch.object(pages, "remember_user", wraps=pages.remember_user) as remember:
            asyncio.run(tui_fake.chat()._pump_keepalive_input(tui_fake.WS(MOM), session))
        self.assertEqual(remember.call_count, 1)

    def test_the_record_speaks_even_with_another_page_open(self):
        connect(OWNER)
        pages.remember_user(SID, MOM)
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_a_record_without_an_opener_is_nobody(self):
        pages.remember_user(SID, "")
        self.assertIsNone(turn._page_sender(SID))

    def test_a_terminal_connection_does_not_overwrite_the_page(self):
        pages.remember_user(SID, MOM)
        pages.remember(SID, tui_fake.Transport("tui"))
        self.assertEqual(pages.recorded(SID), MOM)

    def test_the_end_forgets_the_session(self):
        pages.remember_user(SID, MOM)
        turn.on_session_end(session_id=SID)
        self.assertIsNone(pages.recorded(SID))

    def test_a_page_after_the_end_records_again(self):
        pages.remember_user(SID, MOM)
        turn.on_session_end(session_id=SID)
        page = self.note(MOM)
        page.write({"params": {"type": "message.start", "session_id": SID}})
        self.assertEqual(pages.recorded(SID), MOM)

    def test_a_reset_forgets_the_old_session(self):
        pages.remember_user(SID, MOM)
        turn.on_session_reset(session_id="new", old_session_id=SID)
        self.assertIsNone(pages.recorded(SID))

    def test_two_session_ids_differing_only_in_the_separator_keep_their_own_page(self):
        pages.remember_user("a/b", MOM)
        pages.remember_user("a_b", OWNER)
        self.assertEqual(pages.recorded("a/b"), MOM)
        self.assertEqual(pages.recorded("a_b"), OWNER)

    def test_a_record_is_not_looked_up_again_on_every_frame(self):
        page = self.note(MOM)
        frames = ({"params": {"type": "message.start", "session_id": SID}},)
        page.write(frames[0])
        with mock.patch.object(pages, "recorded", wraps=pages.recorded) as looked:
            page.write(frames[0])
            page.write(frames[0])
        self.assertEqual(looked.call_count, 0)
        # 记录被删掉（会话结束）以后还要能写回来。
        pages.forget(SID)
        page.write(frames[0])
        self.assertEqual(pages.recorded(SID), MOM)

    def test_a_broken_optional_patch_keeps_the_identity_and_says_so(self):
        self.upstream = tui_fake.wire()
        tui_fake.WSTransport.__init__ = ORIGINAL_INIT
        with mock.patch.object(pages, "_patch_pty", side_effect=RuntimeError("boom")):
            with self.assertLogs("wonton.pages", "ERROR"):
                self.assertTrue(pages.install(turn))
        self.assertIs(turn._S["server"], self.upstream.server)
        connect(MOM)
        self.assertEqual(turn._page_sender(SID), MOM)

    def test_a_failed_install_puts_the_connection_class_back(self):
        self.upstream = tui_fake.wire()
        tui_fake.WSTransport.__init__ = ORIGINAL_INIT
        before = self.upstream.ws.WSTransport.__init__
        with mock.patch.object(turn, "attach_gateway", side_effect=RuntimeError("boom")):
            with self.assertLogs("wonton.pages", "ERROR"):
                self.assertFalse(pages.install(turn))
        self.assertIs(self.upstream.ws.WSTransport.__init__, before)


    def test_a_reload_points_the_hook_at_the_new_module(self):
        calls = []
        stub = types.ModuleType("turn_stub")
        stub.attach_gateway = lambda server: calls.append("attach")
        stub.note_page = lambda: calls.append("note_page")
        stub.note_peer = lambda sid, transport: calls.append("note_peer")
        pages.install(lambda: stub)
        connect(MOM)
        self.assertEqual(calls, ["attach", "note_page", "note_peer"])


class NoHomeCase(unittest.TestCase):
    """没有 ``HERMES_HOME`` 时（``tests/test_turn.py`` 就不设）不该炸，网页只是认不出人。"""

    def setUp(self):
        self.upstream = reset()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERMES_HOME", None)

    def test_forget_without_a_home_is_a_no_op(self):
        pages.forget(SID)

    def test_the_end_clears_the_rest_even_without_a_home(self):
        turn._S["active"][SID] = {"sender": MOM}
        turn._S["origins"][SID] = {"session_key": "k"}
        turn._S["unknown"].update({SID, ""})
        turn._S["reported"].add(SID)
        turn.on_session_end(session_id=SID)
        self.assertEqual(turn._S["active"], {})
        self.assertEqual(turn._S["origins"], {})
        self.assertEqual(turn._S["unknown"], set())
        self.assertEqual(turn._S["reported"], set())


if __name__ == "__main__":
    unittest.main()
