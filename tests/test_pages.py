"""网页连接带着打开者：只有网关写过头的连接换身份，其余照旧；这一轮认最后提交的连接。

上游替身见 ``tui_fake``，真形状在 ``tests/hermes`` 里跑。
"""

import sys
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


class WebIdentityCase(unittest.TestCase):
    def setUp(self):
        self.upstream = reset()

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

    def test_a_page_that_closed_is_not_used(self):
        mom = connect(MOM)
        mom.close()
        self.assertIsNone(turn._page_sender(SID))

    def test_a_closed_page_hands_over_to_the_one_still_open(self):
        mom = connect(MOM)
        connect(OWNER)
        mom.close()
        self.assertEqual(turn._page_sender(SID), OWNER)

    def test_the_terminal_tab_speaks_last_but_a_page_after_it_wins_again(self):
        connect(MOM)
        self.upstream.server._rebind_live_transport(SID, tui_fake.session(SID), tui_fake.Transport("tui"))
        self.assertIsNone(turn._page_sender(SID))
        connect(OWNER)
        self.assertEqual(turn._page_sender(SID), OWNER)

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
