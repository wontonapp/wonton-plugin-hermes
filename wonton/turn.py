"""每轮的身份头和调用链、停链和出箱。

云吞核心靠请求头知道 MCP 工具调用替谁说话、属于哪一轮：``X-Wonton-On-Behalf-Of`` 是说话人，
``wonton-session`` 是这一轮的会话，``app.wonton/origin`` 的 ``_meta`` 给房间和话题。Hermes 的 MCP
客户端每轮不带这些，所以在工具处理器的调用上加；值来自工具调用自己 context 的
``HERMES_SESSION_ID``（见下）。

开轮时向核心报链（``POST <核心>/agent/chains/<令牌>``，带 Bot 的 Matrix 令牌），核心按说话人验证，
被拒时这一轮就以那条原因结束、不再推理；结束报 ``End``。核心停链时往出箱
（``GET/POST <核心>/agent/outbox/<令牌>``）放 ``CancelTurn``，这里实时取、打断对应会话并回报；
批准过的私聊消息（``Message``）由这里用 Bot 的 Matrix 客户端发出，出箱项 ID 当 Matrix 事务 ID，
核心重发同一条不会发两遍。

身份靠工具调用自己 context 里的 ``HERMES_SESSION_ID``：上游在 Agent 线程的 context 里按这一轮绑好它，
MCP 的工具线程拷着那份 context 跑，所以拿到的是这一轮自己的会话号，同时几轮也不会串。
``pre_llm_call`` 在别的线程、拷的上下文里跑，它设的 contextvar 回不到工具线程（0.22.7 实测 3/3 次工具调用
都走会话环境那条），所以身份按会话号查 ``_S["active"]`` 和 ``_S["origins"]`` 两张表，不靠“最近一轮”的进程级值。
只有会话号时带 ``wonton-session``、不带代表头，让核心按会话去认；连会话号也取不到才不带身份。

定时任务跑的时候没有人当场说话，会话环境里也只有 Hermes 自己生成的 cron 会话号，``_identity`` 查不到；
这时按插件上下文的 ``current_cron_execution()`` 认出手上的任务，读它的 origin 当说话人（见 ``_cron_identity``）。
委派出去的子代理里上游让它返回 None，改读上游自己的执行标记，子代理同样替创建者说话。
定时任务是上游从 cron 直接进 Agent 的，没有网关消息，所以工具处理器也在 ``pre_llm_call`` 里包。

上游没有插件能挂的“每次工具调用”钩子，只能包它内部的处理器调用；升级 Hermes 时先复查
``tools.mcp_tool_handlers._call_tool_racing_stdio_death`` 和 ``mcp.client.session.ClientSession.call_tool``。
"""

import asyncio
import concurrent.futures
import contextvars
import logging
import os
import threading
from urllib.parse import urlsplit

import httpx

from .locale import language
from .texts import CHAIN_DENIED, CHAIN_LIMIT, CHAIN_LOOP, CHAIN_OTHER, CHAIN_SELF, CHAIN_STOPPED

logger = logging.getLogger(__name__)

ON_BEHALF_OF = "X-Wonton-On-Behalf-Of"
SESSION = "wonton-session"
BOT_TOKEN = "X-Wonton-Bot-Token"
ORIGIN = "app.wonton/origin"

# 报链开始的超时：pre_llm_call 钩子要在这里面拿到准信，才决定这一轮做不做。
START_TIMEOUT = 5.0
# 工具处理器在 MCP 循环上跑，身份在处理器外面 set，包着这次调用：仍然是个 contextvar，但在
# 每次调用自己的任务里 set，值跟着任务走，不看进程里最近是哪一轮。
_CURRENT = contextvars.ContextVar("wonton_turn", default=None)
# 报链结束走后台单线程：钩子在网关事件循环里，同步请求会把所有会话卡住。
_CHAIN_POSTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="wonton-chain")

_S = {
    "loop": None,
    "runner": None,
    "session_store": None,
    "ctx": None,     # 插件上下文：定时任务执行中从它拿这条执行
    "origins": {},   # 会话 id -> {sender, session_key, room, thread, event_id}
    "pending": {},   # 会话键 -> 同上，会话 id 这时还没有
    "active": {},    # 会话 id -> 准了的这一轮的 {sender, session, room, thread}
    "reported": set(),  # 这一轮已经报过链开始的会话 id
    "ending": set(),    # 结束时要报 End 的会话 id（准了和被拒的都要报）
    "unknown": set(),   # 已经提醒过“身份取不到”的会话环境值；空串在每轮结束时重新提醒
    "worker": None,
    "warned": False,
}


def register(ctx):
    _S["ctx"] = ctx
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


def _session_var(name):
    """会话环境里的一项。

    上游把它存在 contextvar 里，网关按这一轮在 Agent 线程的 context 里绑定，MCP 的工具线程拷着那份
    context 跑，所以看到的是这一轮自己的值。这一份 context 里从没绑过时（``_UNSET``），上游回落到
    ``os.environ`` 的旧值；清掉是绑成空串，这时拿到的是空串，不再回落。
    """
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return os.environ.get(name, "")
    return get_session_env(name)


def _session_id():
    return _session_var("HERMES_SESSION_ID")


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
        "source": source,
        "session_key": session_key,
        "room": room,
        "thread": getattr(source, "thread_id", None) or None,
        "event_id": getattr(event, "message_id", None) or getattr(event, "id", None),
    }
    if sid:
        _S["origins"][sid] = found
        _S["reported"].discard(sid)
    if session_key:
        _S["pending"][session_key] = found
    _start_worker()
    return None


def _origin(session_id):
    found = _S["origins"].get(session_id)
    if found is not None:
        # 同一条会话的下一句：记在 pending 里的是这一句的消息和来源，盖掉上一轮的
        # （被打断或重置以后 peek 不到会话 id，pre_gateway_dispatch 只写 pending）。
        fresh = _S["pending"].pop(found.get("session_key"), None)
        if fresh is not None:
            found.update(fresh)
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


def _identity(session_id):
    """这一轮替谁说话，连同这份身份是哪来的。

    按工具调用自己的会话号查这一轮的来源；说话人取不到时不做猜测，带会话号不带代表头，让核心按会话去认
    ——猜错了卡片就挂到别人的链上。会话号也没有时返回 None，调用不带身份（核心的审计里看得到）。

    会话结束以后迟到的调用只剩会话号这一支：核心对已结束的会话没有在跑的轮，会退到“封顶最严的轮”，
    这条会话再开新链之前可能拿到别的说话人，和完全不带会话头一样，不是准的。
    """
    active = _S["active"].get(session_id)
    if active is not None:
        return active, "会话环境"
    origin = _S["origins"].get(session_id)
    if origin is not None:
        return {
            "sender": origin.get("sender"),
            "session": session_id,
            "room": origin.get("room"),
            "thread": origin.get("thread"),
        }, "来源记录"
    if session_id:
        return {"sender": None, "session": session_id, "room": None, "thread": None}, "只有会话"
    return None, "无"


def _cron_execution():
    """手上的定时任务执行，没有时 None。

    先问插件上下文；委派出去的子代理里上游特意让它返回 None（``cron/execution_identity.py``），
    这时退回读上游自己那份执行标记：子代理跑在委派时拷来的 context 里，标记还在，它替同一件事干活。
    """
    ctx = _S["ctx"]
    execution = ctx.current_cron_execution() if ctx is not None else None
    if execution is not None:
        return execution
    try:
        from cron.execution_identity import _CURRENT

        return _CURRENT.get()
    except Exception:
        return None


def _cron_identity():
    """定时任务（含它委派出去的子代理）调工具时替这条任务的创建者说话。没有定时任务时 None。

    定时任务跑的时候没有人当场说话，`_identity` 查不到；任务的 ``origin`` 记着建任务那一轮的会话环境
    （上游 ``tools/cronjob_job_args._origin_from_env``），``origin`` 是建任务时从会话环境抄的字符串，
    A 让 Bot 建的任务就按 A 算，模型在工具子进程里改不了它。

    头是插件自报的，核心只核对“这个 Bot 听不听这个人”（和聊天轮一样）；创建者后来不听这个 Bot 了，
    核心按认不出算、照旧只读，这里不猜。平台不是正好 ``matrix``、没有创建者、任务没了都不带身份。
    定时任务没有进行中的轮次，子代理也不是一轮，所以只带代表头，不带 ``wonton-session``。
    """
    execution = _cron_execution()
    if execution is None:
        return None
    try:
        from cron.jobs import get_job

        job = get_job(execution.job_id)
    except Exception as exc:
        logger.warning("读定时任务 %s 失败，调用不带身份：%r", execution.job_id, exc)
        return None
    origin = (job or {}).get("origin") or {}
    sender = origin.get("user_id")
    if not sender or origin.get("platform") != "matrix":
        return None
    return {"sender": sender, "room": origin.get("chat_id"), "thread": origin.get("thread_id")}


def pre_llm_call(session_id="", sender_id="", **_):
    """报链开始；被拒时用这条原因结束这一轮。一条消息只报一次。"""
    # 定时任务没有网关消息，pre_gateway_dispatch 不会来，只靠这里把工具处理器包上。
    _install_headers()
    origin = _origin(session_id) or {}
    sender = sender_id or origin.get("sender")
    if not sender or not origin.get("room") or session_id in _S["reported"]:
        return None
    body = {
        "phase": "start",
        "session": session_id,
        "room": origin.get("room") or "",
        "sender": sender,
        "event_id": origin.get("event_id") or "",
    }
    if origin.get("thread"):
        body["thread"] = origin["thread"]
    verdict, refusal, detail = _chain_start(body)
    if verdict == "stopped":
        # 报到的链已经停了（唤醒标签还指着它，常见于主人刚按下停止）：去掉消息 id 再报一次，
        # 核心就按会话新开一条链。停了的链本来也不接新消息，叫回同样开新链。
        body["event_id"] = ""
        verdict, refusal, detail = _chain_start(body)
    if verdict == "retry":
        return None
    _S["reported"].add(session_id)
    if verdict == "grant":
        _S["active"][session_id] = {
            "sender": sender,
            "session": session_id,
            "room": origin.get("room"),
            "thread": origin.get("thread"),
        }
        _S["ending"].add(session_id)
        return None
    if verdict == "stopped":
        return {"action": "block", "message": CHAIN_STOPPED[_language(sender)]}
    # 其他拒绝：链没开成，也要报 End，让核心把这轮放掉。
    _S["ending"].add(session_id)
    logger.info("核心不收这一轮（%r）：%s", refusal, detail)
    return {"action": "block", "message": _refusal_text(refusal, sender, detail)}


def _language(sender):
    """说话人的语言；问不到时英文。钩子跑在单独的线程里，那里没有事件循环。"""
    coro = language(sender)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception as exc:
            logger.debug("取语言失败，按英文回：%r", exc)
            return "en"
    # 调用线程里已经有事件循环（钩子改成在循环里跑也照常）：借一次性线程跑完再等。
    box = {}

    def run():
        try:
            box["lang"] = asyncio.run(coro)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        logger.debug("取语言失败，按英文回：%r", box["error"])
        return "en"
    return box["lang"]


def _refusal_text(refusal, sender, detail=""):
    """核心的拒绝类别换成给说话人看的一句话。

    类别说不清的地方（``invalid_input`` 没给 refusal、``other`` 什么都可能是）把核心给的说明接在后面。
    会话挂在别的链上、卡片已登记这类核心错误走的是 ``other``，类型分类只在响应头 ``x-wonton-error``
    里，插件只读响应体，所以落到下面泛泛的那句。
    """
    lang = _language(sender)
    kind = refusal.get("type")
    if kind == "permission_denied":
        text = CHAIN_DENIED[lang]
        return f"{text}\n\n{detail}" if detail else text
    if kind == "limit_reached":
        return CHAIN_LIMIT[lang]
    if kind == "invalid_input" and refusal.get("refusal") == "self_call":
        return CHAIN_SELF[lang]
    if kind == "invalid_input" and refusal.get("refusal") == "loop":
        return CHAIN_LOOP[lang]
    text = CHAIN_OTHER[lang]
    return f"{text}\n\n{detail}" if detail else text


def on_session_end(session_id="", **_):
    """这一轮结束：清身份，报过的链报结束。"""
    if _S["ending"] and session_id not in _S["ending"]:
        logger.warning("一轮结束报不回去 session=%s，手里还留着 %r", session_id, sorted(_S["ending"]))
    _S["active"].pop(session_id, None)
    _S["unknown"].discard(session_id)
    _S["unknown"].discard("")
    _S["reported"].discard(session_id)
    origin = _S["origins"].pop(session_id, None) or {}
    _S["pending"].pop(origin.get("session_key"), None)
    if session_id in _S["ending"]:
        _S["ending"].discard(session_id)
        _post_chain({"phase": "end", "session": session_id})


def _chain_start(body):
    """同步报链开始，返回 ``(结论, 拒绝体, 核心的说明)``。

    结论是 ``grant``（准了）、``stopped``、``refused``，或者 ``retry``（没拿到准信，下一次模型调用再报）。
    """
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        return "retry", {}, ""
    try:
        response = httpx.post(f"{core}/agent/chains/{token}", json=body,
                              headers={BOT_TOKEN: bot}, timeout=START_TIMEOUT, trust_env=False)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("报链开始失败，下一次模型调用再报：%r", exc)
        return "retry", {}, ""
    detail = response.headers.get("X-Wonton-Detail", "")
    if response.is_success:
        return "grant", {}, detail
    try:
        refusal = response.json()
    except ValueError:
        refusal = None
    if not isinstance(refusal, dict) or not refusal.get("type"):
        logger.warning("报链开始被拒（%s），原因没认出来：%s", response.status_code, detail)
        return "retry", {}, detail
    return ("stopped" if refusal["type"] == "stopped" else "refused"), refusal, detail


def _post_chain(body):
    """排队 POST 一轮的结束；失败只记日志。"""
    core, token = _core()
    bot = _bot_token()
    if not core or not token or not bot:
        return
    _CHAIN_POSTS.submit(_chain_request, f"{core}/agent/chains/{token}", body, bot)


def _chain_request(url, body, bot):
    try:
        response = httpx.post(url, json=body, headers={BOT_TOKEN: bot}, timeout=START_TIMEOUT, trust_env=False)
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
        env = _session_id()
        turn, source = _identity(env)
        logger.debug("MCP 调用的身份取自%s：环境的会话 %r，带的 %r", source, env, turn)
        if turn is None or turn.get("sender") is None:
            cron = _cron_identity()
            if cron is not None:
                turn, source = cron, "定时任务"
                logger.debug("MCP 调用来自定时任务：替创建者 %s 说话，不带会话", turn["sender"])
            elif turn is not None and _cron_execution() is not None:
                # 定时任务的会话号核心认不出，别拿它去猜别的轮。
                turn = {**turn, "session": None}
        if turn is None:
            if source != "轮次":
                logger.debug("MCP 调用的身份取自%s：环境的会话 %r，带的 %r", source, env, turn)
            if env not in _S["unknown"]:
                _S["unknown"].add(env)
                if env:
                    logger.warning("这一轮的身份取不到（环境的会话 %r）：调用不带代表头和会话", env)
                else:
                    logger.warning("这一轮没有会话号，身份取不到：调用不带代表头和会话")
            return await original(server, *args, **kwargs)
        _patch_session(server.session)
        token = _CURRENT.set(turn)
        try:
            return await original(server, *args, **kwargs)
        finally:
            _CURRENT.reset(token)

    call._wonton_turn = True
    mcp_tool_handlers._call_tool_racing_stdio_death = call


def _stamp_turn(session):
    """让会话在每次请求的头里带上这一轮的身份。

    上游把 resolve_param_headers 在 ``adopt()`` 时就包进 ``_stamp`` 闭包，之后再换 ``_resolve_param_headers``
    不起作用；``_stamp`` 每次请求现调，改它才落到线上。``adopt()`` 会换掉 ``_stamp``，包过以后跟着重包。
    """
    stamp = session._stamp
    if getattr(stamp, "_wonton_stamp", False):
        return

    def with_turn(data, opts):
        stamp(data, opts)
        turn = _CURRENT.get()
        if turn and data.get("method") == "tools/call":
            headers = opts.setdefault("headers", {})
            if turn.get("sender"):
                headers[ON_BEHALF_OF] = turn["sender"]
            if turn.get("session"):
                headers[SESSION] = turn["session"]

    with_turn._wonton_stamp = True
    session._stamp = with_turn


def _patch_session(session):
    if getattr(session, "_wonton_turn", False):
        return
    session._wonton_turn = True
    _stamp_turn(session)
    original_adopt = session.adopt

    def adopt(result):
        original_adopt(result)
        _stamp_turn(session)

    session.adopt = adopt
    original_call = session.call_tool

    async def call_tool(name, arguments=None, *args, **kwargs):
        turn = _CURRENT.get()
        if turn and "meta" not in kwargs:
            origin = {name: turn[key] for key, name in (("room", "room_id"), ("thread", "thread_id")) if turn.get(key)}
            if origin:
                # 核心的 origin 要求 room_id；只有会话号时塞空的 {} 反而解析不了，不带。
                kwargs["meta"] = {ORIGIN: origin}
        return await original_call(name, arguments, *args, **kwargs)

    session.call_tool = call_tool


def _start_worker():
    """起后台的出箱轮询；任务留在 ``_S`` 里，事件循环只对任务持弱引用。"""
    loop = _S["loop"]
    worker = _S["worker"]
    if worker and (not hasattr(worker, "done") or not worker.done()):
        return
    if loop is None or loop.is_closed():
        return
    _S["worker"] = loop.create_task(_outbox_loop())


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
    response = await client.get(f"{core}/agent/outbox/{token}", headers={BOT_TOKEN: bot})
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
    response = await client.post(f"{core}/agent/outbox/{token}", timeout=5,
                                 json={"id": item_id, "result": result}, headers={BOT_TOKEN: bot})
    response.raise_for_status()


def _release_slot(runner, session_key):
    """打断这一轮但不依赖它的清会话钩子：只放槽，走在跑的上游驱逐同一对调用。"""
    generation = runner._interrupt_running_turn(
        session_key,
        interrupt_reason="云吞停链",
        invalidation_reason="wonton_chain_stopped",
    )
    runner._drop_turn_slot(session_key, run_generation=generation)


async def _do_outbox(item, client=None):
    item_id = item.get("id")
    kind = item.get("kind") or {}
    if kind.get("type") == "cancel_turn":
        session = kind.get("session") or ""
        origin = _origin(session) or {}
        session_key = origin.get("session_key")
        source = origin.get("source")
        runner = _runner()
        if session_key and runner is not None:
            clear = getattr(runner, "_interrupt_and_clear_session", None)
            try:
                if clear is not None and source is not None:
                    # 和 /stop 走同一条路：光打断不停用那一轮的槽，槽一直占着，之后的消息全被当成“忙”排队，没人放。
                    await clear(
                        session_key,
                        source,
                        interrupt_reason="云吞停链",
                        invalidation_reason="wonton_chain_stopped",
                    )
                else:
                    _release_slot(runner, session_key)
            except Exception as exc:
                # 清会话时抛了异常也要把槽放掉；核心不重发 CancelTurn，槽占着就再也轮不到这一会话。
                logger.warning("停链时清会话失败 %s：%r", session, exc)
                try:
                    _release_slot(runner, session_key)
                except Exception as slot_exc:
                    logger.warning("停链时放轮次槽也失败 %s：%r", session, slot_exc)
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
