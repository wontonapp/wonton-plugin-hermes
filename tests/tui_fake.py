"""上游 ``tui_gateway`` 的最小替身：连接、会话表、提交时的那次 rebind。

只留 ``wonton.pages`` 用到的部分，行为照着 0.22.7 抄；真长度和形状由 ``tests/hermes`` 钉住。用它跑的用例
不依赖上游包。
"""

import sys
import types

from wonton import pages

GATEWAY = "gateway-entry"  # 主机主人经网关进来的那份身份（鉴权盖的）


class Transport:
    def __init__(self, name="tui"):
        self._closed = False
        self.auth_identity = None
        self.name = name

    def close(self):
        self._closed = True


class WSTransport(Transport):
    def __init__(self, ws, loop, *, peer="unknown", auth_identity=None):
        super().__init__()
        self._ws = ws
        self.loop = loop
        self.peer = peer
        self.auth_identity = auth_identity
        self.frames = []

    def write(self, obj):
        self.frames.append(obj)
        return True

    async def write_async(self, obj):
        return self.write(obj)


class FanoutTransport:
    def __init__(self, *transports):
        self._transport_list = list(transports)

    def transports(self):
        return list(self._transport_list)

    def attach(self, transport):
        self._transport_list.append(transport)
        return True

    def detach(self, transport):
        self._transport_list.remove(transport)


class WS:
    """上游的 starlette WebSocket：头在 ``headers`` 里（小写键）。"""

    def __init__(self, opener=None, extra=None):
        self.headers = {"x-hermes-auth": "ticket"}
        if opener:
            self.headers[pages.HEADER] = opener
        self.headers.update(extra or {})
        self.state = types.SimpleNamespace()


sessions = {}
_installed = False


def wire():
    """把替身装进 ``sys.modules``，返回能直接用的模块组。已装过就只重置会话表。"""
    global _installed
    sessions.clear()
    if _installed:
        return modules()
    pw = types.ModuleType("tui_gateway.pw")
    ws = types.ModuleType("tui_gateway.ws")
    ws.WSTransport = WSTransport

    life = types.ModuleType("tui_gateway.session_lifecycle")

    def rebind(session_id, session, transport):
        attach(session, transport)
        session.setdefault("viewers", {})[transport] = 1

    life._rebind_live_transport = rebind

    tr = types.ModuleType("tui_gateway.session_transports")

    def is_dead(transport):
        return getattr(transport, "_closed", None) is True

    def is_live_peer(transport):
        return transport is not None and not is_dead(transport)

    def live_transports(session):
        transport = (session or {}).get("transport")
        if transport is None:
            return []
        peers = transport.transports() if hasattr(transport, "transports") else [transport]
        return [peer for peer in peers if is_live_peer(peer)]

    def attach(session, transport):
        existing = session.get("transport")
        if existing is transport:
            return True
        if isinstance(existing, FanoutTransport):
            return existing.attach(transport)
        transport = FanoutTransport(existing, transport) if existing else transport
        session["transport"] = transport
        return True

    tr._sessions = sessions
    tr._transport_is_dead = is_dead
    tr._transport_is_live_peer = is_live_peer
    tr._session_live_transports = live_transports
    tr._attach_session_transport = attach
    tr._detach_session_transport = lambda session, t: session["transport"].detach(t)

    server = types.ModuleType("tui_gateway.server")
    server._sessions = sessions
    server._session_live_transports = live_transports
    server._rebind_live_transport = rebind  # 调用方走的是 server 上这份，和上游 rebind 后的情形一样

    chat = types.ModuleType("hermes_cli.web_routers.chat_ws")

    async def pump(ws, session):
        """照上游：把客户端发来的每段原样交给 ``session.write``。"""
        while True:
            raw = await session.receive()
            if raw is None:
                return
            if not await session.write(ws, raw):
                return

    pump.pump = pump
    chat._pump_keepalive_input = pump

    web = types.ModuleType("hermes_cli.web_routers")
    web.chat_ws = chat
    cli = types.ModuleType("hermes_cli")
    cli.web_routers = web

    gateway = types.ModuleType("tui_gateway")
    gateway.pw = pw
    gateway.ws = ws
    gateway.session_lifecycle = life
    gateway.session_transports = tr
    gateway.server = server
    for name, module in {
        "tui_gateway": gateway,
        "tui_gateway.pw": pw,
        "tui_gateway.ws": ws,
        "tui_gateway.session_lifecycle": life,
        "tui_gateway.session_transports": tr,
        "tui_gateway.server": server,
        "hermes_cli": cli,
        "hermes_cli.web_routers": web,
        "hermes_cli.web_routers.chat_ws": chat,
    }.items():
        sys.modules[name] = module
    _installed = True
    return modules()


def modules():
    return sys.modules["tui_gateway"]


def chat():
    return sys.modules["hermes_cli.web_routers.chat_ws"]


def install(turn):
    """重置替身并装补丁。返回补丁后的上游模块。"""
    wire()
    assert pages.install(lambda: turn), "补丁装不上"
    return modules()


def session(sid):
    return sessions.setdefault(sid, {"session_id": sid, "transport": None})


class PTY:
    """``/chat`` 那条终端连接：上游带 ``active_session_file`` 的会话对象，收到什么都交给 ``write``。"""

    def __init__(self, sid, path):
        self.active_session_file = path
        self.written = []
        self.input = []
        (path).write_text('{"session_id": "%s"}' % sid, encoding="utf-8")

    async def receive(self):
        return self.input.pop(0) if self.input else None

    async def write(self, ws, raw):
        self.written.append(raw)
        return True
