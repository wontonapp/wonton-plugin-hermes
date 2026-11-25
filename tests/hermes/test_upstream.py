"""钉住依赖的上游形状（Hermes 0.23.4）：控制台的 WebSocket 入口、身份传到连接类、会话表里认得住来源，
以及终端那条连接替哪个会话收键、身份怎么落在连接上。

镜像自己记着版本（``/opt/hermes/install-stamp.json``）。版本一变先来这里看：``wonton/pages.py`` 认的就是
这几处，形状对不上时要改它。
"""

import inspect
import json
import sys
import types
import unittest
from pathlib import Path

from hermes_cli import pty_session
from hermes_cli.web_routers import chat_ws
from tui_gateway import server, session_lifecycle, session_reaper, session_transports, ws

PIN = "0.23.4"
REBIND = None


def setUpModule():
    global REBIND
    # 已经装过就拆回原件；单独跑本文件时自己装一次，补丁会用 __wrapped__ 留着原件
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from wonton import pages
        REBIND = getattr(server._rebind_live_transport, "__wrapped__", server._rebind_live_transport)
        pages.install(lambda: types.SimpleNamespace(
            attach_gateway=lambda *_: None, note_page=lambda *_: None, note_peer=lambda *_: None))
    finally:
        sys.path.remove(str(Path(__file__).resolve().parents[2]))


def params(function):
    return list(inspect.signature(function).parameters)


class UpstreamCase(unittest.TestCase):
    def test_the_image_is_the_version_we_pinned(self):
        stamp = json.loads(Path("/opt/hermes/install-stamp.json").read_text())
        self.assertEqual(stamp["displayVersion"], PIN,
                         f"上游不是 {PIN}：复核 wonton/pages.py 认的结构和本文件，再改这里")

    def test_the_console_socket_is_where_we_think(self):
        paths = [route.path for route in chat_ws.router.routes]
        self.assertIn("/api/ws", paths)
        source = inspect.getsource(chat_ws.gateway_ws)
        self.assertIn("_hermes_auth_identity", source)
        self.assertLess(source.index("_close_unless_sidecar_allowed"), source.index("_hermes_auth_identity"))
        # 网关自己不碰请求头：打开者头原样留在连接上，只有核心能决定它的值
        self.assertNotIn("ws.headers", source)

    def test_the_identity_reaches_the_transport(self):
        self.assertIn("auth_identity", params(ws.handle_ws))
        source = inspect.getsource(ws.handle_ws)
        self.assertIn("WSTransport(", source)
        self.assertIn("auth_identity=auth_identity", source)

    def test_the_transport_takes_the_identity_last(self):
        signature = inspect.signature(ws.WSTransport.__init__)
        self.assertEqual(params(ws.WSTransport.__init__)[1], "ws")
        self.assertEqual(signature.parameters["auth_identity"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(signature.parameters["auth_identity"].default)

    def test_every_submit_and_resume_rebinds_with_the_senders_transport(self):
        self.assertEqual(params(session_lifecycle._rebind_live_transport)[:3], ["sid", "session", "transport"])
        self.assertIn("_attach_session_transport(session, transport)",
                      inspect.getsource(session_lifecycle._rebind_live_transport))

    def test_callers_use_the_rebound_copy_not_the_module_function(self):
        # 各模块被重新绑到 server 的全局变量上，调用方走 server 那份；补丁打原件不起作用
        self.assertEqual(REBIND.__globals__, vars(server))
        self.assertIsNot(REBIND, session_lifecycle._rebind_live_transport)
        # 补丁盖在 server 上，原件留在 __wrapped__ 里继续用
        self.assertIs(server._rebind_live_transport.__wrapped__, REBIND)

    def test_the_session_table_says_which_transports_are_live(self):
        self.assertIsInstance(server._sessions, dict)
        self.assertEqual(params(server._session_live_transports), ["session"])
        self.assertIn("_transport_is_live_peer",
                      inspect.getsource(server._session_live_transports))
        self.assertIn("_closed", inspect.getsource(session_reaper._transport_is_dead))

    def test_the_terminal_socket_tells_which_session_the_page_keeps(self):
        self.assertIn("/api/pty", [route.path for route in chat_ws.router.routes])
        self.assertEqual(params(chat_ws._pump_keepalive_input), ["ws", "session"])
        self.assertIn("session.write(ws, raw)", inspect.getsource(chat_ws._pump_keepalive_input))
        self.assertIn("active_session_cleanup", inspect.getsource(pty_session.PtySession))

    def test_the_session_knows_its_active_session_file(self):
        self.assertIn("active_session_file", params(pty_session.PtySession.__init__))
        marker = inspect.getsource(chat_ws._pty_channel_marker_state)
        self.assertIn("_active_session_file_for_channel", marker)
        read = inspect.getsource(chat_ws._read_active_session_file)
        self.assertIn("session_id", read)

    def test_the_transport_can_tell_us_the_session_of_a_frame(self):
        self.assertEqual(params(ws.WSTransport.write), ["self", "obj"])
        self.assertEqual(params(ws.WSTransport.write_async), ["self", "obj"])
