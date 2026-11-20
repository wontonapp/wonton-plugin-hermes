"""网页控制台里的对话替打开者说话：让网页连接带上核心盖的打开者。

云吞核心把插件网页转给 Hermes 之前，在升级请求上写一条 ``X-Wonton-Web-User``（值是打开者的 Matrix
用户 ID），转发前先剥掉客户端自己带的同名头。开在云吞 App 里的终端页签也是经核心转发、由设备认出人的，
同样带头；只有真走 ``x-hermes-auth`` 内部凭据、不经核心的连接（``provider`` 为 ``server-internal``）没有。

上游 ``gateway_ws`` 鉴权后盖一份主机主人的身份，连同连接一起交给 ``WSTransport``。补丁打在这里、存下
身份之后换掉 ``user_id``：会话归属照旧（上游对有已认证身份的一律算主机主人），认人只落在这条连接上，
按轮现取（``turn._page_sender``），连接关了没人再用。

会话那边的 ``_rebind_live_transport`` 每次提交、恢复都带着提交者的连接进来，顺手记下：同一会话开着两个
浏览器时，这一轮算最后提交的那个人。它在 server 上有一份换过全局变量的副本，调用方走的是那份，补丁也打在
那份上（``tui_gateway/server.py`` 末尾重新绑定了各模块，模块里的原件没有再被调到）。

控制台的连接和这一轮跑的地方不是同一个进程：``hermes dashboard`` 拿着 ``WSTransport``，回复和工具在
``hermes gateway run`` 里跑，``pre_llm_call`` 也在那边。进程内存各是各的，打开者只能落盘交接：
``$HERMES_HOME/wonton-web-users/<会话号>`` 写着谁，网关那边的 ``turn._page_sender`` 就读谁。两边共用
``HERMES_HOME``。写的人有两个：``/chat`` 的终端连接每收一次键，就按上游子的活动会话文件把这一页记到
那会话上；``note_peer`` 带着提交的连接再记一次（网页控制台里的对话从网关进来时它在那一侧被调到）。同一会话
开着两页时后写的算数。会话结束时删掉。

两处都是上游内部结构（0.22.7），``tests/hermes/test_upstream.py`` 钉着；取不到时网页回到只读，不认错人。
"""

import functools
import importlib
import json
import logging
import os
import pathlib
import tempfile

logger = logging.getLogger(__name__)

HEADER = "x-wonton-web-user"
# 身份上的来源标记：只认带它的连接，别的（TUI、终端、内部凭据）不掺和。
PROVIDER = "wonton"
_MARK = "_wonton_pages"


def install(turn_of):
    """给上游连接类装上打开者。``turn_of`` 拿插件里管网页身份的那份代码（模块或返回它的函数）。装上了返回 True。"""
    if not callable(turn_of):
        target = turn_of
        turn_of = lambda: target  # noqa: E731
    try:
        ws = importlib.import_module("tui_gateway.ws")
        server = importlib.import_module("tui_gateway.server")
    except Exception as exc:
        logger.error("上游没有控制台入口，网页对话会按只读：%r", exc)
        return False
    original = ws.WSTransport.__init__
    try:
        if getattr(original, _MARK, False):
            # 热重载过：钩子跑在新模块里，补丁还指着旧模块，得重新绑；连接类的 __init__ 不必再包一层。
            turn_of().attach_gateway(server)
            _patch_rebind(server, turn_of)
            return True
        ws.WSTransport.__init__ = _init(original, turn_of)
        _patch_writes(ws.WSTransport)
        _patch_pty()
        turn_of().attach_gateway(server)
        _patch_rebind(server, turn_of)
    except Exception as exc:
        logger.error("上游的连接或会话表换了形状，网页对话会按只读：%r", exc)
        return False
    return True


def _patch_writes(transport_class):
    """这条连接替哪个会话推事件，就是这一页开着的会话：出帧时把会话号记到打开者名下。

    会话号只有网关知道，控制台这边能看见的只有推给这一页的事件帧（``params.session_id``）；
    打开者又只在打开页面的那条连接上。两样一对上就写一条记录，``turn._page_sender`` 在网关那边按会话号读。
    同一页只记换过的会话，逐帧写字不划算。
    """
    for name in ("write", "write_async"):
        original = getattr(transport_class, name)
        if getattr(original, _MARK, False):
            continue

        @functools.wraps(original)
        def wrapped(self, obj, *args, __original=original, **kwargs):
            _sniff(self, obj)
            return __original(self, obj, *args, **kwargs)

        setattr(wrapped, _MARK, True)
        setattr(transport_class, name, wrapped)


def _patch_pty():
    """``/chat`` 的终端连接在 dashboard 里：它替哪个会话收键，就把这一页记到那个会话名下。

    这一页的会话号是子进程写进 ``session.active_session_file`` 的，随键到达现读，不缓存、不轮询。
    终端的连接本身没有打开者，也不是发出对话的那条连接，只用来给「这一页现在开着哪条会话」对上号。
    """
    try:
        chat_ws = importlib.import_module("hermes_cli.web_routers.chat_ws")
        original = chat_ws._pump_keepalive_input
    except Exception as exc:
        logger.error("控制台的终端入口换了位置，终端页签那边认不出这一页的主人：%r", exc)
        return
    if getattr(original, _MARK, False):
        return

    @functools.wraps(original)
    async def pump(ws, session, *args, **kwargs):
        opener = _opener(ws)
        path = getattr(session, "active_session_file", None)
        if opener and path is not None:
            noted_id = {}
            _note_marker(path, opener, noted_id)
            write = session.write

            async def noted(ws_, raw, __write=write):
                _note_marker(path, opener, noted_id)
                return await __write(ws_, raw)

            session.write = noted
        return await original(ws, session, *args, **kwargs)

    setattr(pump, _MARK, True)
    chat_ws._pump_keepalive_input = pump


def _note_marker(path, opener, noted_id):
    """把终端连接正对着的会话记给``opener``；换过会话才重记，文件没写会话号时什么都不做。"""
    try:
        session_id = json.loads(path.read_text(encoding="utf-8")).get("session_id")
    except (OSError, ValueError):
        return
    if not isinstance(session_id, str) or not session_id:
        return
    if noted_id.get("id") == session_id and recorded(session_id) is not None:
        return
    noted_id["id"] = session_id
    remember_user(session_id, opener)


def _sniff(transport, frame):
    if not isinstance(frame, dict):
        return
    params = frame.get("params")
    session_id = params.get("session_id") if isinstance(params, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return
    if getattr(transport, "_wonton_served", None) == session_id and recorded(session_id) is not None:
        return
    try:
        remember(session_id, transport)
    except Exception as exc:
        logger.error("记不下这一页开着哪条会话：%r", exc)
    transport._wonton_served = session_id


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


def remember(session_id, transport):
    """``remember_user``，人从连接上取。"""
    remember_user(session_id, opener_of(transport))


def remember_user(session_id, opener):
    """记下``会话号``是``opener``开着的；没有打开者的连接不盖掉已经记下的人。"""
    if not session_id:
        return
    directory = _directory()
    if directory is None:
        return
    if not opener and recorded(session_id) not in (None, ""):
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(opener or "")
        os.replace(tmp, directory / _file_name(session_id))
    except OSError as exc:
        logger.error("记不下这一轮是哪个网页连接提交的，会按只读：%r", exc)


def recorded(session_id):
    """``remember`` 写下的打开者：没记过是 None，记过没人提交是空串。"""
    directory = _directory()
    if directory is None or not session_id:
        return None
    try:
        return (directory / _file_name(session_id)).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("读不回这一轮是哪个网页连接提交的，按还开着的取：%r", exc)
        return None


def forget(session_id):
    directory = _directory()
    try:
        (directory / _file_name(session_id)).unlink()
    except (AttributeError, FileNotFoundError):
        pass
    except OSError as exc:
        logger.warning("没扫掉 %s 的提交者记录：%r", session_id, exc)


def opener_of(transport):
    """这条连接带的打开者；不是网页连接时 None。"""
    identity = getattr(transport, "auth_identity", None)
    if not isinstance(identity, dict) or identity.get("provider") != PROVIDER:
        return None
    return identity.get("user_id") or None


def _directory():
    home = os.environ.get("HERMES_HOME")
    return pathlib.Path(home) / "wonton-web-users" if home else None


def _file_name(session_id):
    return session_id.replace("/", "_").replace(os.sep, "_")
