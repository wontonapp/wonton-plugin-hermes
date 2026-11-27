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

``tools/list`` 也盖这组头：核心按说话人封顶广告出去的清单，连接、重连时取的清单会挂着连接那一刻的帽子，
所以每轮模型调用前按这一轮的说话人重取一遍，再把新工具交给这一轮。

上游没有插件能挂的“每次工具调用”钩子，只能包它内部的处理器调用；升级 Hermes 时先复查
``tools.mcp_tool_handlers._call_tool_racing_stdio_death``、``mcp.client.session.ClientSession.call_tool`` /
``list_tools``、``tools.mcp_tool._servers`` 和 ``tools.mcp_tool_agent.refresh_agent_mcp_tools``。
"""

import asyncio
import concurrent.futures
import contextlib
import contextvars
import logging
import os
import sys
import threading
from urllib.parse import urlsplit

import httpx

from .locale import language
from .texts import CHAIN_DENIED, CHAIN_LIMIT, CHAIN_LOOP, CHAIN_OTHER, CHAIN_SELF, CHAIN_STOPPED

logger = logging.getLogger(__name__)

turn = sys.modules[__name__]

ON_BEHALF_OF = "X-Wonton-On-Behalf-Of"
SESSION = "wonton-session"
BOT_TOKEN = "X-Wonton-Bot-Token"
ORIGIN = "app.wonton/origin"

# 报链开始的超时：pre_llm_call 钩子要在这里面拿到准信，才决定这一轮做不做。
START_TIMEOUT = 5.0
# 重取工具清单的超时：也在 pre_llm_call 里，等它把清单交上来，模型才有工具可用。
REFRESH_TIMEOUT = 10.0
# 工具处理器在 MCP 循环上跑，身份在处理器外面 set，包着这次调用：仍然是个 contextvar，但在
# 每次调用自己的任务里 set，值跟着任务走，不看进程里最近是哪一轮。
# 报链结束走后台单线程：钩子在网关事件循环里，同步请求会把所有会话卡住。
_CHAIN_POSTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="wonton-chain")

_S = {
    "loop": None,
    "runner": None,
    "session_store": None,
    "gateway": None,   # 网关：pre_llm_call 的钩子线程上按会话号从它的 agent 缓存找这一轮
    "ctx": None,     # 插件上下文：定时任务执行中从它拿这条执行
    "origins": {},   # 会话 id -> {sender, session_key, room, thread, event_id}
    "pending": {},   # 会话键 -> 同上，会话 id 这时还没有
    "active": {},    # 会话 id -> 准了的这一轮的 {sender, session, room, thread}
    "reported": set(),  # 这一轮已经报过链开始的会话 id
    "ending": set(),    # 结束时要报 End 的会话 id（准了和被拒的都要报）
    "unknown": set(),   # 已经提醒过“身份取不到”的会话环境值；空串在每轮结束时重新提醒
    "cron_origins": {}, # 任务 id -> 身份或 None：一次执行里 origin 不变，读到过就记住
    "cron_warned": {},  # 任务 id -> 提醒过的那份 origin，同一份只提醒一次
    "last": None,       # 这一轮的身份：清单没有进行中的调用，给它用
    "listed": None,     # 清单当前是按谁的封顶取的
    "warned_list": False,
    "warned_marker": False,
    "worker": None,
    "webs": {},      # 会话 id -> 刚提交这条会话的网页连接（见 pages.py）
    "server": None,  # 上游会话表所在的模块，由 pages 装上（gateway 是网关本身，不是它）
    "page_warned": set(),  # 已经报过“上游会话表换了形状”的会话 id
    "dispatched": {},  # 会话 id -> {待认领的发送人: origin}：只有 sender_id 对上的那一轮认领，结束不清、重置才清
    "warned": False,
}


def register(ctx):
    turn._S["ctx"] = ctx
    ctx.register_hook("pre_gateway_dispatch", turn.pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", turn.pre_llm_call)
    ctx.register_hook("on_session_end", turn.on_session_end)
    ctx.register_hook("on_session_reset", turn.on_session_reset)


def pre_gateway_dispatch(event=None, gateway=None, session_store=None, **_):
    """记下这一轮的来源和会话键；第一条消息时起出箱轮询。"""
    try:
        turn._S["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    turn._install_headers()
    if session_store is not None:
        turn._S["session_store"] = session_store
    if gateway is not None:
        turn._S["gateway"] = gateway
    source = getattr(event, "source", None)
    room = getattr(source, "chat_id", None)
    if room is None or not turn._is_matrix(getattr(source, "platform", None)):
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
            turn.logger.debug("算会话键失败：%r", exc)
    found = {
        "sender": sender,
        "source": source,
        "session_key": session_key,
        "room": room,
        "thread": getattr(source, "thread_id", None) or None,
        "event_id": getattr(event, "message_id", None) or getattr(event, "id", None),
    }
    if sid:
        turn._S["origins"][sid] = found
        turn._S["dispatched"].setdefault(sid, {})[sender] = found
        turn._S["reported"].discard(sid)
    if session_key:
        turn._S["pending"][session_key] = found
    turn._start_worker()
    return None


def _claim_dispatch(session_id, sender_id):
    """这一轮是不是网关派发的那条 IM；是就把它自己的来源交出来并清掉标记。

    只认说话人对上的那一条：网页开着这条会话时，先跑到的网页轮、终端页签轮（``sender_id`` 是
    ``uid:<n>``）都不能把标记用掉；同一条会话几条 IM 连着来时，各人认领各人的来源，后一条盖掉
    ``_S["origins"]`` 也不影响前一条。
    """
    if not sender_id:
        return None
    waiting = turn._S["dispatched"].get(session_id)
    if not waiting:
        return None
    found = waiting.pop(sender_id, None)
    if found is None:
        return None
    if not waiting:
        turn._S["dispatched"].pop(session_id, None)
    # 这个人自己那条消息要另报一次：派发进来的下一轮可能赶在上一条还能跑的时候到，``reported`` 会挡住它。
    turn._S["reported"].discard(session_id)
    return found


def pre_llm_call(session_id="", sender_id="", **_):
    """报链开始；被拒时用这条原因结束这一轮。一条消息只报一次。"""
    # 定时任务没有网关消息，pre_gateway_dispatch 不会来，只靠这里把工具处理器包上。
    turn._install_headers()
    # 网关派发的这一轮（IM 消息）按 IM 的人、房间和事件号算：网页开着这条会话时，开页的人也不能顶掉它。
    # 标记按会话号挂在说话人名下，对上的人才认领（控制台是 ``cloud-gateway``，App 页签是 ``uid:<n>``）。
    dispatched = turn._claim_dispatch(session_id, sender_id)
    if dispatched is not None:
        turn._S["origins"][session_id] = dispatched
        origin = dispatched
    else:
        origin = turn._origin(session_id) or {}
    page = None if dispatched is not None else turn._page_sender(session_id)
    if page is not None:
        # 网页轮不带房间和事件号：房间只对 IM 来的那一轮有意义。认不出开页的人（只读连接、没有记录）时
        # 留着来源，和认不出派发标记时一样按 IM 兜底。
        origin = {}
    sender = page or sender_id or origin.get("sender")
    turn._remember_for_listing(session_id, sender)
    if not sender or session_id in turn._S["reported"]:
        return None
    if page is None and not origin.get("room"):
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
    verdict, refusal, detail = turn._chain_start(body)
    if verdict == "stopped":
        # 报到的链已经停了（唤醒标签还指着它，常见于主人刚按下停止）：去掉消息 id 再报一次，
        # 核心就按会话新开一条链。停了的链本来也不接新消息，叫回同样开新链。
        body["event_id"] = ""
        verdict, refusal, detail = turn._chain_start(body)
    if verdict == "retry":
        if dispatched is not None:
            # 下一次模型调用还按这条 IM 算。
            turn._S["dispatched"].setdefault(session_id, {})[sender] = dispatched
        return None
    turn._S["reported"].add(session_id)
    if verdict == "grant":
        turn._S["active"][session_id] = {
            "sender": sender,
            "session": session_id,
            "room": origin.get("room"),
            "thread": origin.get("thread"),
        }
        turn._S["ending"].add(session_id)
        return None
    if verdict == "stopped":
        return {"action": "block", "message": CHAIN_STOPPED[turn._language(sender)]}
    # 其他拒绝：链没开成，也要报 End，让核心把这轮放掉。
    turn._S["ending"].add(session_id)
    turn.logger.info("核心不收这一轮（%r）：%s", refusal, detail)
    return {"action": "block", "message": turn._refusal_text(refusal, sender, detail)}


def _language(sender):
    """说话人的语言；问不到时英文。钩子跑在单独的线程里，那里没有事件循环。"""
    coro = turn.language(sender)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception as exc:
            turn.logger.debug("取语言失败，按英文回：%r", exc)
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
        turn.logger.debug("取语言失败，按英文回：%r", box["error"])
        return "en"
    return box["lang"]


def _refusal_text(refusal, sender, detail=""):
    """核心的拒绝类别换成给说话人看的一句话。

    类别说不清的地方（``invalid_input`` 没给 refusal、``other`` 什么都可能是）把核心给的说明接在后面。
    会话挂在别的链上、卡片已登记这类核心错误走的是 ``other``，类型分类只在响应头 ``x-wonton-error``
    里，插件只读响应体，所以落到下面泛泛的那句。
    """
    lang = turn._language(sender)
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


def on_session_reset(session_id=None, old_session_id=None, **_):
    """会话被重置（``/new`` 等）：旧会话的网页记录和派发标记没人再读，删掉。"""
    turn._S["dispatched"].pop(old_session_id or session_id, None)
    turn._pages().forget(old_session_id or session_id)


def on_session_end(session_id="", **_):
    """这一轮结束：清身份，报过的链报结束。"""
    if turn._S["ending"] and session_id not in turn._S["ending"]:
        turn.logger.warning("一轮结束报不回去 session=%s，手里还留着 %r", session_id, sorted(turn._S["ending"]))
    turn._S["active"].pop(session_id, None)
    turn._S["webs"].pop(session_id, None)
    turn._S["page_warned"].discard(session_id)
    # 派发标记不清：先结束的这一轮（常是网页轮）不能把还没跑的 IM 那一轮吃掉，只有重置才清。
    turn._pages().forget(session_id)
    turn._S["unknown"].discard(session_id)
    turn._S["unknown"].discard("")
    turn._S["reported"].discard(session_id)
    origin = turn._S["origins"].pop(session_id, None) or {}
    turn._S["pending"].pop(origin.get("session_key"), None)
    if session_id in turn._S["ending"]:
        turn._S["ending"].discard(session_id)
        turn._post_chain({"phase": "end", "session": session_id})


def _chain_start(body):
    """同步报链开始，返回 ``(结论, 拒绝体, 核心的说明)``。

    结论是 ``grant``（准了）、``stopped``、``refused``，或者 ``retry``（没拿到准信，下一次模型调用再报）。
    """
    core, token = turn._core()
    bot = turn._bot_token()
    if not core or not token or not bot:
        return "retry", {}, ""
    try:
        response = turn.httpx.post(f"{core}/agent/chains/{token}", json=body,
                              headers={turn.BOT_TOKEN: bot}, timeout=turn.START_TIMEOUT, trust_env=False)
    except (turn.httpx.HTTPError, ValueError) as exc:
        turn.logger.warning("报链开始失败，下一次模型调用再报：%r", exc)
        return "retry", {}, ""
    detail = response.headers.get("X-Wonton-Detail", "")
    if response.is_success:
        return "grant", {}, detail
    try:
        refusal = response.json()
    except ValueError:
        refusal = None
    if not isinstance(refusal, dict) or not refusal.get("type"):
        turn.logger.warning("报链开始被拒（%s），原因没认出来：%s", response.status_code, detail)
        return "retry", {}, detail
    return ("stopped" if refusal["type"] == "stopped" else "refused"), refusal, detail


def _post_chain(body):
    """排队 POST 一轮的结束；失败只记日志。"""
    core, token = turn._core()
    bot = turn._bot_token()
    if not core or not token or not bot:
        return
    turn._CHAIN_POSTS.submit(turn._chain_request, f"{core}/agent/chains/{token}", body, bot)


def _chain_request(url, body, bot):
    try:
        response = turn.httpx.post(url, json=body, headers={turn.BOT_TOKEN: bot}, timeout=turn.START_TIMEOUT, trust_env=False)
        response.raise_for_status()
    except (turn.httpx.HTTPError, ValueError) as exc:
        turn.logger.warning("报调用链 %s 失败：%r", body.get("phase"), exc)


def _start_worker():
    """起后台的出箱轮询；任务留在 ``_S`` 里，事件循环只对任务持弱引用。"""
    loop = turn._S["loop"]
    worker = turn._S["worker"]
    if worker and (not hasattr(worker, "done") or not worker.done()):
        return
    if loop is None or loop.is_closed():
        return
    turn._S["worker"] = loop.create_task(turn._outbox_loop())


async def _outbox_loop():
    delay = 1
    async with turn.httpx.AsyncClient(timeout=40, trust_env=False) as client:
        while True:
            try:
                item = await turn._poll_outbox(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                turn.logger.warning("取出箱失败：%r", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            delay = 1
            if item is None:
                continue
            try:
                await turn._do_outbox(item, client)
            except Exception as exc:
                turn.logger.warning("执行出箱项失败 %r：%r", item.get("id"), exc)
                try:
                    await turn._receipt(item.get("id"), {"result": "failed", "category": "other"}, client)
                except Exception as receipt_exc:
                    turn.logger.warning("回报出箱失败 %r：%r", item.get("id"), receipt_exc)


async def _poll_outbox(client=None):
    core, token = turn._core()
    bot = turn._bot_token()
    if not core or not token or not bot:
        await asyncio.sleep(5)
        return None
    if client is None:
        async with turn.httpx.AsyncClient(timeout=40, trust_env=False) as own:
            return await turn._poll_outbox(own)
    response = await client.get(f"{core}/agent/outbox/{token}", headers={turn.BOT_TOKEN: bot})
    response.raise_for_status()
    return response.json()


async def _receipt(item_id, result, client=None):
    core, token = turn._core()
    bot = turn._bot_token()
    if not core or not token or not bot or not item_id:
        return
    if client is None:
        async with turn.httpx.AsyncClient(timeout=5, trust_env=False) as own:
            return await turn._receipt(item_id, result, own)
    response = await client.post(f"{core}/agent/outbox/{token}", timeout=5,
                                 json={"id": item_id, "result": result}, headers={turn.BOT_TOKEN: bot})
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
        origin = turn._origin(session) or {}
        session_key = origin.get("session_key")
        source = origin.get("source")
        runner = turn._runner()
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
                    turn._release_slot(runner, session_key)
            except Exception as exc:
                # 清会话时抛了异常也要把槽放掉；核心不重发 CancelTurn，槽占着就再也轮不到这一会话。
                turn.logger.warning("停链时清会话失败 %s：%r", session, exc)
                try:
                    turn._release_slot(runner, session_key)
                except Exception as slot_exc:
                    turn.logger.warning("停链时放轮次槽也失败 %s：%r", session, slot_exc)
            await turn._receipt(item_id, {"result": "sent", "event_id": ""}, client)
        else:
            await turn._receipt(item_id, {"result": "failed", "category": "other"}, client)
    elif kind.get("type") == "message":
        event_id, category = await turn._send(
            kind.get("recipient") or "", kind.get("body") or "", item_id
        )
        if event_id is None:
            await turn._receipt(item_id, {"result": "failed", "category": category}, client)
        else:
            await turn._receipt(item_id, {"result": "sent", "event_id": event_id}, client)
    else:
        await turn._receipt(item_id, {"result": "failed", "category": "other"}, client)


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
    client = turn._client()
    server = turn._server()
    if client is None or not server or not recipient:
        return None, "other"
    target = f"@{recipient}:{server}"
    try:
        direct = await client.get_account_data("m.direct") or {}
        joined = set(await client.get_joined_rooms() or [])
    except Exception as exc:
        turn.logger.warning("读私聊房间失败：%r", exc)
        return None, "other"
    for room in direct.get(target, []):
        if room in joined:
            from mautrix.types import EventType

            try:
                event_id = await asyncio.wait_for(client.send_message_event(
                    room, EventType.ROOM_MESSAGE,
                    {"msgtype": "m.text", "body": body}, txn_id=txn_id), timeout=45)
            except Exception as exc:
                turn.logger.warning("发私聊失败：%r", exc)
                return None, "other"
            return event_id, ""
    return None, "no_room"


def _client():
    runner = turn._runner()
    if runner is None:
        return None
    try:
        from gateway.config import Platform

        adapter = runner.adapters.get(Platform.MATRIX)
    except Exception as exc:
        turn.logger.warning("拿不到 Matrix 适配器：%r", exc)
        return None
    return getattr(adapter, "_client", None)


# 子模块经 ``turn.X`` 取共享名；这里把原来的名字导回来，对外的导入一个不少。
from .turn_env import (  # noqa: E402
    _bot_token,
    _core,
    _is_matrix,
    _runner,
    _session_id,
    _session_var,
)
from .turn_identity import (  # noqa: E402
    _cron_execution,
    _cron_identity,
    _cron_identity_async,
    _cron_key,
    _identity,
    _origin,
    _page_sender,
    _page_user,
    _pages,
    _warn_cron,
    _warn_pages,
    attach_gateway,
    note_page,
    note_peer,
)
from .turn_headers import (  # noqa: E402
    _CURRENT,
    _install_headers,
    _patch_session,
    _stamp_turn,
    _wrap_list_tools,
)
from .turn_listing import (  # noqa: E402
    _agent_for_turn,
    _refresh_agent_tools,
    _refresh_tools,
    _remember_for_listing,
)
