"""网页连接带着打开者：只有网关写过头的连接换身份，其余照旧；这一轮认最后提交的连接。

上游替身见 ``tui_fake``，真形状在 ``tests/hermes`` 里跑。
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tui_fake  # noqa: E402
from wonton import pages, turn  # noqa: E402

SID = "web-session"
MOM = "@mom:example.org"
OWNER = "@owner:example.org"
GATEWAY = {"user_id": "cloud-gateway", "provider": "local", "profile_id": "p1"}


def reset():
    turn._S.update({"loop": None, "worker": True, "webs": {}, "gateway": None, "server": None, "page_warned": set(),
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
        turn._S["gateway"] = object()  # 没有 _sessions
        turn._page_sender(SID)
        with self.assertLogs("wonton.turn", "WARNING"):
            turn._page_sender("other")

    def test_no_upstream_session_table_is_a_debug_round_not_the_one_shot(self):
        turn._S["gateway"] = None
        with self.assertLogs("wonton.turn", "DEBUG") as logs:
            self.assertIsNone(turn._page_sender(SID))
            self.assertIsNone(turn._page_sender(SID))
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(turn._S["page_warned"], set())

    def test_a_changed_upstream_shape_warns_once_per_session(self):
        turn._S["gateway"] = object()  # 没有 _sessions
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
            turn.pre_llm_call(session_id=SID, sender_id="cloud-gateway")
        sender, kind = turn._identity(SID)
        self.assertEqual(sender["sender"], MOM)
        self.assertIsNone(sender["room"])
        self.assertEqual(kind, "会话环境")

    def test_a_page_turn_starts_the_chain_without_a_room(self):
        connect(MOM)
        bodies = []
        with mock.patch.object(turn, "_chain_start", lambda body: (bodies.append(dict(body)) or ("grant", "", ""))):
            turn.pre_llm_call(session_id=SID, sender_id="cloud-gateway")
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


if __name__ == "__main__":
    unittest.main()
