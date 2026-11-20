"""网页控制台里的对话替打开者说话：让网页连接带上核心盖的打开者。

云吞核心把插件网页转给 Hermes 之前，在升级请求上写一条 ``X-Wonton-Web-User``（值是打开者的 Matrix
用户 ID），转发前先剥掉客户端自己带的同名头。开在云吞 App 里的终端页签走内部凭据，没有这条头。

上游 ``gateway_ws`` 鉴权后盖一份主机主人的身份，连同连接一起交给 ``WSTransport``。补丁打在这里、存下
身份之后换掉 ``user_id``：会话归属照旧（上游对有已认证身份的一律算主机主人），认人只落在这条连接上，
按轮现取（``turn._page_sender``），连接关了没人再用。

会话那边的 ``_rebind_live_transport`` 每次提交、恢复都带着提交者的连接进来，顺手记下：同一会话开着两个
浏览器时，这一轮算最后提交的那个人。它在 server 上有一份换过全局变量的副本，调用方走的是那份，补丁也打在
那份上（``tui_gateway/server.py`` 末尾重新绑定了各模块，模块里的原件没有再被调到）。

两处都是上游内部结构（0.22.7），``tests/hermes/test_upstream.py`` 钉着；取不到时网页回到只读，不认错人。
"""

import functools
import importlib
import logging

logger = logging.getLogger(__name__)

HEADER = "x-wonton-web-user"
# 身份上的来源标记：只认带它的连接，别的（TUI、终端、内部凭据）不掺和。
PROVIDER = "wonton"
_MARK = "_wonton_pages"


def install(turn_of):
    """给上游连接类装上打开者。``turn_of`` 拿插件里管网页身份的那份代码。装上了返回 True。"""
    try:
        ws = importlib.import_module("tui_gateway.ws")
        server = importlib.import_module("tui_gateway.server")
    except Exception as exc:
        logger.error("上游没有控制台入口，网页对话会按只读：%r", exc)
        return False
    original = ws.WSTransport.__init__
    if getattr(original, _MARK, False):
        return True
    ws.WSTransport.__init__ = _init(original, turn_of)
    turn_of().attach_gateway(server)
    _patch_rebind(server, turn_of)
    return True


def _init(original, turn_of):
    @functools.wraps(original)
    def init(self, ws, *args, **kwargs):
        original(self, ws, *args, **kwargs)
        opener = _opener(ws)
        if opener:
            identity = dict(self.auth_identity or {})
            identity.update(user_id=opener, provider=PROVIDER)
            self.auth_identity = identity
        try:
            turn_of().note_page()
        except Exception as exc:
            logger.error("网页连接交给 turn 失败，出箱轮询和 MCP 头会缺：%r", exc)
    setattr(init, _MARK, True)
    return init


def _patch_rebind(server, turn_of):
    try:
        original = server._rebind_live_transport
    except AttributeError as exc:
        logger.warning("上游没有 _rebind_live_transport，同一会话开着两个浏览器时按还开着的取：%r", exc)
        return
    if getattr(original, "_wonton_rebind", False):
        return

    @functools.wraps(original)
    def rebind(sid, session, transport, *args, **kwargs):
        result = original(sid, session, transport, *args, **kwargs)
        try:
            turn_of().note_peer(sid, transport)
        except Exception as exc:
            logger.error("记不下这一轮是哪个网页连接提交的：%r", exc)
        return result

    rebind._wonton_rebind = True
    server._rebind_live_transport = rebind


def _opener(ws):
    headers = getattr(ws, "headers", None)
    try:
        return (headers.get(HEADER) or "").strip() or None
    except Exception:
        return None
