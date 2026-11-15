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
import concurrent.futures
import contextvars
import logging
import os
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

ON_BEHALF_OF = "X-Wonton-On-Behalf-Of"
SESSION = "wonton-session"
BOT_TOKEN = "X-Wonton-Bot-Token"
ORIGIN = "app.wonton/origin"

# 工具处理器在别的线程跑，提交时上游会复制 contextvars，所以钩子在这里 set 的值处理器看得到。
_CURRENT = contextvars.ContextVar("wonton_turn", default=None)
# 报链在后台单线程顺序发：钩子跑在网关事件循环里，同步请求会把所有会话卡住；单线程保证 start 先于 end。
_CHAIN_POSTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="wonton-chain")

_S = {
    "loop": None,
    "runner": None,
    "session_store": None,
    "origins": {},   # 会话 id -> {sender, session_key, room, thread, event_id}
    "pending": {},   # 会话键 -> 同上，会话 id 这时还没有
    "tokens": {},    # 会话 id -> contextvar 的 reset 凭据
    "worker": False,
    "warned": False,
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
    _post_chain(body)
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
    origin = _S["origins"].pop(session_id, None) or {}
    _S["pending"].pop(origin.get("session_key"), None)
    _post_chain({"phase": "end", "session": session_id})


def _post_chain(body):
    """排队 POST 一轮的开始/结束；失败只记日志，不挡对话。"""
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        return
    url = f"{core}/mcp/{token}/agent/chains"
    _CHAIN_POSTS.submit(_chain_request, url, body, bot)


def _chain_request(url, body, bot):
    try:
        response = httpx.post(url, json=body, headers={BOT_TOKEN: bot}, timeout=5, trust_env=False)
        response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("报调用链 %s 失败：%r", body.get("phase"), exc)


def _install_headers():
    """给 MCP 会话的每次 ``tools/call`` 带上这一轮的身份头和来源。

    插件注册时 Hermes 还没导 ``tools.mcp_tool_handlers``，第一条消息到时再包；包过以后这里是空的。
    """
    try:
        from tools import mcp_tool_handlers
    except Exception as exc:
        logger.debug("包 MCP 工具处理器失败：%r", exc)
        return
    try:
        original = mcp_tool_handlers._call_tool_racing_stdio_death
    except AttributeError as exc:
        if not _S["warned"]:
            _S["warned"] = True
            logger.warning("上游没有 MCP 工具处理器 hook 点，身份头不会带上：%r", exc)
        return
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
    delay = 1
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        while True:
            try:
                item = await _poll_outbox(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("取出箱失败：%r", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            delay = 1
            if item is None:
                continue
            try:
                await _do_outbox(item, client)
            except Exception as exc:
                logger.warning("执行出箱项失败 %r：%r", item.get("id"), exc)
                try:
                    await _receipt(item.get("id"), {"result": "failed", "category": "other"}, client)
                except Exception as receipt_exc:
                    logger.warning("回报出箱失败 %r：%r", item.get("id"), receipt_exc)


async def _poll_outbox(client=None):
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        await asyncio.sleep(5)
        return None
    if client is None:
        async with httpx.AsyncClient(timeout=40, trust_env=False) as own:
            return await _poll_outbox(own)
    response = await client.get(f"{core}/mcp/{token}/agent/outbox", headers={BOT_TOKEN: bot})
    response.raise_for_status()
    return response.json()


async def _receipt(item_id, result, client=None):
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot or not item_id:
        return
    if client is None:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as own:
            return await _receipt(item_id, result, own)
    await client.post(f"{core}/mcp/{token}/agent/outbox", timeout=5,
                      json={"id": item_id, "result": result}, headers={BOT_TOKEN: bot})


async def _do_outbox(item, client=None):
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
            await _receipt(item_id, {"result": "sent", "event_id": ""}, client)
        else:
            await _receipt(item_id, {"result": "failed", "category": "other"}, client)
    elif kind.get("type") == "message":
        event_id, category = await _send(
            kind.get("recipient") or "", kind.get("body") or "", item_id
        )
        if event_id is None:
            await _receipt(item_id, {"result": "failed", "category": category}, client)
        else:
            await _receipt(item_id, {"result": "sent", "event_id": event_id}, client)
    else:
        await _receipt(item_id, {"result": "failed", "category": "other"}, client)


def _server():
    """收件人的服务器：优先 ``MATRIX_HOMESERVER`` 的主机名，取不到再认 Bot 的 MXID。"""
    homeserver = os.environ.get("MATRIX_HOMESERVER", "")
    if homeserver:
        host = urlsplit(homeserver).hostname
        if host:
            return host
    return os.environ.get("MATRIX_USER_ID", "").partition(":")[2]


async def _send(recipient, body, txn_id):
    """以 Bot 的身份发进和收件人的私聊。

    返回 ``(event_id, category)``；没发成时 event_id 为 None，category 是 ``no_room``（没有已加入的私聊）
    或 ``other``（读账号数据、发送本身失败）。
    """
    client = _client()
    server = _server()
    if client is None or not server or not recipient:
        return None, "other"
    target = f"@{recipient}:{server}"
    try:
        direct = await client.get_account_data("m.direct") or {}
        joined = set(await client.get_joined_rooms() or [])
    except Exception as exc:
        logger.warning("读私聊房间失败：%r", exc)
        return None, "other"
    for room in direct.get(target, []):
        if room in joined:
            from mautrix.types import EventType

            try:
                event_id = await asyncio.wait_for(client.send_message_event(
                    room, EventType.ROOM_MESSAGE,
                    {"msgtype": "m.text", "body": body}, txn_id=txn_id), timeout=45)
            except Exception as exc:
                logger.warning("发私聊失败：%r", exc)
                return None, "other"
            return event_id, ""
    return None, "no_room"


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
