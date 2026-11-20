"""真 Hermes 控制台里，网关写下的打开者盖过登录身份，保留其余字段；没头的连接照旧只读。

只在镜像里跑（``tests/hermes``）；上游结构由 ``test_upstream`` 钉着。
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from hermes_cli.web_server import app
from hermes_cli.web_routers import chat_ws
from tui_gateway import ws as ws_module

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from wonton import pages, turn  # noqa: E402

MOM = "@mom:example.org"
LOGIN = {"user_id": "/owner", "provider": "local", "profile_id": "p1", "instance_id": "i1"}


def route():
    found = [r for r in app.router.routes if getattr(r, "path", None) == "/api/ws"]
    assert len(found) == 1, [r.path for r in app.router.routes if "ws" in getattr(r, "path", "")]
    return found[0]


class WS:
    def __init__(self, opener=None):
        self.headers = {"x-hermes-auth": "ticket"}
        if opener:
            self.headers[pages.HEADER] = opener
        self.app = types.SimpleNamespace(state=types.SimpleNamespace())
        self.scope = {}
        self._hermes_auth_identity = dict(LOGIN)


REBIND = None


def setUpModule():
    global REBIND
    from tui_gateway import server

    REBIND = server._rebind_live_transport  # 上游原来那份，补丁会盖掉 server 上的
    assert pages.install(lambda: turn)


class RouteCase(unittest.IsolatedAsyncioTestCase):
    def test_the_route_we_call_is_the_one_mounted(self):
        self.assertEqual([r.path for r in app.router.routes].count("/api/ws"), 1)

    def setUp(self):
        turn._S.update({"loop": None, "worker": True, "webs": {}, "server": None, "page_warned": set()})

    async def call(self, opener):
        seen, transports = [], []

        async def handle(ws, **kwargs):
            # 照 handle_ws 的做法建连接，身份就在同一个参数上交给它
            seen.append(kwargs)
            transports.append(ws_module.WSTransport(ws, asyncio.get_running_loop(), auth_identity=kwargs["auth_identity"]))

        gate = mock.patch.object(chat_ws, "_close_unless_sidecar_allowed", mock.AsyncMock(return_value=True))
        startup = mock.patch("hermes_cli.mcp_startup.start_deferred_mcp_discovery_now", lambda: None)
        handler = mock.patch.object(ws_module, "handle_ws", handle)
        with gate, startup, handler:
            await chat_ws.gateway_ws(WS(opener))
        self.assertTrue(seen, "上游没走到 handle_ws")
        return transports[0]

    def test_the_warning_set_takes_a_session(self):
        turn._warn_pages("s1", "boom")
        self.assertIn("s1", turn._S["page_warned"])

    async def test_the_opener_from_the_gateway_wins(self):
        # 核心在转发前剥掉了客户端自己带的同名头、写下打开者；插件照单全收，不碰登录身份的其他字段
        identity = (await self.call(MOM)).auth_identity
        self.assertEqual(identity["user_id"], MOM)
        self.assertEqual(identity["provider"], "wonton")
        self.assertEqual(identity["profile_id"], "p1")
        self.assertEqual(identity["instance_id"], "i1")
        self.assertEqual(identity["profile_scope"], "host")

    async def test_without_the_header_the_login_identity_stays(self):
        identity = (await self.call(None)).auth_identity
        self.assertEqual(identity, {**LOGIN, "profile_scope": "host"})

    async def test_the_transport_carries_the_opener_into_the_session(self):
        transport = ws_module.WSTransport(WS(MOM), asyncio.get_running_loop(), auth_identity=dict(LOGIN))
        self.assertEqual(transport.auth_identity["user_id"], MOM)
        turn.note_peer("s1", transport)
        self.assertEqual(turn._page_sender("s1"), MOM)


class DashboardCase(unittest.TestCase):
    def test_the_dashboard_loads_our_file_as_the_plugin_backend(self):
        # 控制台只认 dashboard/manifest.json 的 api；按文件路径 exec，不靠包名
        api = ROOT / "wonton" / "dashboard" / "api.py"
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_wonton", api)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        decoy = types.ModuleType("hermes_plugins.wonton.turn")
        decoy.__file__ = str(ROOT / "wonton" / "turn.py")
        with mock.patch.dict(sys.modules, {"hermes_plugins.wonton.turn": decoy, "hermes_plugins.wonton": None}):
            sys.modules.pop("hermes_plugins.wonton")
            self.assertEqual(module.plugin().__name__, "wonton")
        self.assertEqual(module.root, ROOT / "wonton" / "__init__.py")
        self.assertFalse(module.router.routes)
