"""每轮的身份头和调用链、停链和出箱。

云吞核心靠请求头知道 MCP 工具调用替谁说话、属于哪一轮：``X-Wonton-On-Behalf-Of`` 是说话人，
``wonton-session`` 是这一轮的会话，``app.wonton/origin`` 的 ``_meta`` 给房间和话题。Hermes 的 MCP
客户端每轮不带这些，所以在工具处理器的调用上加；值来自钩子记下的当前一轮。

开轮时向核心报链（``POST <核心>/agent/chains``，带 Bot 的 Matrix 令牌），核心按说话人验证；结束回
``End``。核心停链时往出箱（``GET/POST …/agent/outbox``）放 ``CancelTurn``，这里实时取、打断对应会话
并回报；批准过的私聊消息（``Message``）由这里用 Bot 的 Matrix 客户端发出，出箱项 ID 当 Matrix 事务
ID，核心重发同一条不会发两遍。

上游没有插件能挂的“每次工具调用”钩子，只能包它内部的处理器调用；升级 Hermes 时先复查
``tools/mcp_tool_handlers._call_tool_racing_stdio_death`` 和 ``mcp.client.session.ClientSession.call_tool``。
"""

import asyncio
import contextvars
import logging
import os

import httpx

logger = logging.getLogger(__name__)

ON_BEHALF_OF = "X-Wonton-On-Behalf-Of"
SESSION = "wonton-session"
BOT_TOKEN = "X-Wonton-Bot-Token"
ORIGIN = "app.wonton/origin"

# 工具处理器在别的线程跑，提交时上游会复制 contextvars，所以钩子在这里 set 的值处理器看得到。
_CURRENT = contextvars.ContextVar("wonton_turn", default=None)

_S = {
    "loop": None,
    "runner": None,
    "session_store": None,
    "origins": {},   # 会话 id -> {sender, session_key, room, thread, event_id}
    "pending": {},   # 会话键 -> 同上，会话 id 这时还没有
    "tokens": {},    # 会话 id -> contextvar 的 reset 凭据
    "worker": False,
}


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_end", on_session_end)


def _core():
    """核心地址和这个插件的总线令牌：MCP 地址是 ``<核心>/mcp/<令牌>``。拿不到时 None。"""
    url = os.environ.get("WONTON_MCP_URL", "")
    head, sep, token = url.rpartition("/mcp/")
    if not sep or not head or not token:
        return None, None
    return head.rstrip("/"), token


def _bot_token():
    return os.environ.get("MATRIX_ACCESS_TOKEN", "")


def _is_matrix(platform):
    return str(getattr(platform, "value", platform) or "").lower() == "matrix"


def _runner():
    if _S["runner"] is not None:
        return _S["runner"]
    try:
        from gateway.run import _gateway_runner_ref

        _S["runner"] = _gateway_runner_ref()
    except Exception as exc:
        logger.warning("拿不到网关：%r", exc)
    return _S["runner"]


def pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_):
    """记下这一轮的来源和会话键；第一条消息时起出箱轮询。"""
    try:
        _S["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    _install_headers()
    if session_store is not None:
        _S["session_store"] = session_store
    source = getattr(event, "source", None)
    room = getattr(source, "chat_id", None)
    if room is None or not _is_matrix(getattr(source, "platform", None)):
        return None
    sender = getattr(source, "user_id", None)
    if not sender:
        return None
    session_key = None
    sid = None
    if session_store is not None:
        try:
            session_key = session_store._generate_session_key(source)
            sid = session_store.peek_session_id(session_key)
        except Exception as exc:
            logger.debug("算会话键失败：%r", exc)
    found = {
        "sender": sender,
        "session_key": session_key,
        "room": room,
        "thread": getattr(source, "thread_id", None) or None,
        "event_id": getattr(event, "message_id", None) or getattr(event, "id", None),
        "fresh": True,
    }
    if sid:
        _S["origins"][sid] = found
    if session_key:
        _S["pending"][session_key] = found
    _start_worker()
    return None


def _origin(session_id):
    found = _S["origins"].get(session_id)
    if found is not None:
        return found
    store = _S["session_store"]
    if store is None or not session_id:
        return None
    try:
        entry = store.lookup_by_session_id(session_id)
    except Exception as exc:
        logger.debug("查会话来源失败 %s：%r", session_id, exc)
        return None
    origin = getattr(entry, "origin", None) if entry else None
    if origin is None or not _is_matrix(getattr(origin, "platform", None)):
        return None
    try:
        session_key = store._generate_session_key(origin)
    except Exception:
        session_key = None
    found = _S["pending"].pop(session_key, None) or {}
    found.update({
        "sender": getattr(origin, "user_id", None),
        "session_key": session_key,
        "room": getattr(origin, "chat_id", None),
        "thread": getattr(origin, "thread_id", None) or None,
    })
    found.setdefault("event_id", None)
    _S["origins"][session_id] = found
    return found


def pre_llm_call(session_id="", sender_id="", **_):
    """把这一轮的身份放进 contextvar，报链开始。一条消息内每次模型调用都过这里，链只报一次。"""
    origin = _origin(session_id) or {}
    sender = sender_id or origin.get("sender")
    if not sender:
        return None
    if session_id in _S["tokens"] and not origin.get("fresh"):
        return None
    current = {
        "sender": sender,
        "session": session_id,
        "room": origin.get("room"),
        "thread": origin.get("thread"),
    }
    _S["tokens"][session_id] = _CURRENT.set(current)
    body = {
        "phase": "start",
        "session": session_id,
        "room": origin.get("room") or "",
        "sender": sender,
        "event_id": origin.get("event_id") or "",
    }
    if origin.get("thread"):
        body["thread"] = origin["thread"]
    chain = _post_chain(body)
    if chain:
        current["chain"] = chain
    origin["fresh"] = False
    return None


def on_session_end(session_id="", interrupted=False, **_):
    """这一轮结束：清身份，报链结束。"""
    token = _S["tokens"].pop(session_id, None)
    if token is not None:
        try:
            _CURRENT.reset(token)
        except ValueError:
            _CURRENT.set(None)
    _S["origins"].pop(session_id, None)
    _post_chain({"phase": "end", "session": session_id}, background=True)


def _post_chain(body, background=False):
    """POST 一轮的开始/结束；失败只记日志，不挡对话。开始返回链 ID。"""
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        return None
    url = f"{core}/mcp/{token}/agent/chains"
    if background:
        loop = _S["loop"]
        if loop is None or loop.is_closed():
            return None
        asyncio.run_coroutine_threadsafe(asyncio.to_thread(_chain_request, url, body, bot), loop)
        return None
    try:
        response = httpx.post(url, json=body, headers={BOT_TOKEN: bot}, timeout=5, trust_env=False)
        response.raise_for_status()
        return response.json().get("chain")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("报调用链失败 %r：%r", body.get("phase"), exc)
        return None


def _chain_request(url, body, bot):
    try:
        httpx.post(url, json=body, headers={BOT_TOKEN: bot}, timeout=5, trust_env=False)
    except httpx.HTTPError as exc:
        logger.warning("报调用链结束失败：%r", exc)


def _install_headers():
    """给 MCP 会话的每次 ``tools/call`` 带上这一轮的身份头和来源。

    插件注册时 Hermes 还没导 ``tools.mcp_tool_handlers``，第一条消息到时再包；包过以后这里是空的。
    """
    try:
        from tools import mcp_tool_handlers
    except Exception as exc:
        logger.debug("包 MCP 工具处理器失败：%r", exc)
        return
    original = mcp_tool_handlers._call_tool_racing_stdio_death
    if getattr(original, "_wonton_turn", False):
        return

    async def call(server, *args, **kwargs):
        turn = _CURRENT.get()
        if turn:
            _patch_session(server.session)
        return await original(server, *args, **kwargs)

    call._wonton_turn = True
    mcp_tool_handlers._call_tool_racing_stdio_death = call


def _patch_session(session):
    if getattr(session, "_wonton_turn", False):
        return
    session._wonton_turn = True
    original_headers = session._resolve_param_headers

    def resolve(name, arguments):
        headers = dict(original_headers(name, arguments) or {})
        turn = _CURRENT.get()
        if turn:
            headers[ON_BEHALF_OF] = turn["sender"]
            headers[SESSION] = turn["session"]
        return headers

    session._resolve_param_headers = resolve
    original_call = session.call_tool

    async def call_tool(name, arguments=None, *args, **kwargs):
        turn = _CURRENT.get()
        if turn and "meta" not in kwargs:
            origin = {name: turn[key] for key, name in (("room", "room_id"), ("thread", "thread_id")) if turn.get(key)}
            kwargs["meta"] = {ORIGIN: origin}
        return await original_call(name, arguments, *args, **kwargs)

    session.call_tool = call_tool


def _start_worker():
    loop = _S["loop"]
    if _S["worker"] or loop is None or loop.is_closed():
        return
    _S["worker"] = True
    loop.create_task(_outbox_loop())


async def _outbox_loop():
    while True:
        try:
            item = await _poll_outbox()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("取出箱失败：%r", exc)
            await asyncio.sleep(1)
            continue
        if item is None:
            continue
        try:
            await _do_outbox(item)
        except Exception as exc:
            logger.warning("执行出箱项失败 %r：%r", item.get("id"), exc)
            await _receipt(item.get("id"), {"result": "failed", "category": "other"})


async def _poll_outbox():
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        await asyncio.sleep(5)
        return None
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        response = await client.get(f"{core}/mcp/{token}/agent/outbox", headers={BOT_TOKEN: bot})
        response.raise_for_status()
        return response.json()


async def _receipt(item_id, result):
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot or not item_id:
        return
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        await client.post(f"{core}/mcp/{token}/agent/outbox",
                          json={"id": item_id, "result": result}, headers={BOT_TOKEN: bot})


async def _do_outbox(item):
    item_id = item.get("id")
    kind = item.get("kind") or {}
    if kind.get("type") == "cancel_turn":
        session = kind.get("session") or ""
        origin = _S["origins"].get(session) or {}
        session_key = origin.get("session_key")
        runner = _runner()
        if session_key and runner is not None:
            runner._interrupt_running_turn(
                session_key,
                interrupt_reason="云吞停链",
                invalidation_reason="wonton_chain_stopped",
            )
        await _receipt(item_id, {"result": "sent", "event_id": ""})
    elif kind.get("type") == "message":
        event_id = await _send(kind.get("recipient") or "", kind.get("body") or "", item_id)
        if event_id is None:
            await _receipt(item_id, {"result": "failed", "category": "no_room"})
        else:
            await _receipt(item_id, {"result": "sent", "event_id": event_id})
    else:
        await _receipt(item_id, {"result": "failed", "category": "other"})


async def _send(recipient, body, txn_id):
    """以 Bot 的身份发进和收件人的私聊；没有可用私聊发不成时 None。"""
    client = _client()
    user = os.environ.get("MATRIX_USER_ID", "")
    _, _, server = user.partition(":")
    if client is None or not server or not recipient:
        return None
    target = f"@{recipient}:{server}"
    try:
        direct = await client.get_account_data("m.direct") or {}
        joined = set(await client.get_joined_rooms() or [])
    except Exception as exc:
        logger.warning("读私聊房间失败：%r", exc)
        return None
    for room in direct.get(target, []):
        if room in joined:
            from mautrix.types import EventType

            return await client.send_message_event(
                room, EventType.ROOM_MESSAGE,
                {"msgtype": "m.text", "body": body}, txn_id=txn_id)
    return None


def _client():
    runner = _runner()
    if runner is None:
        return None
    try:
        from gateway.config import Platform

        adapter = runner.adapters.get(Platform.MATRIX)
    except Exception as exc:
        logger.warning("拿不到 Matrix 适配器：%r", exc)
        return None
    return getattr(adapter, "_client", None)
