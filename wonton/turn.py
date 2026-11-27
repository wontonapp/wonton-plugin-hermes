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
import logging
import sys

import httpx  # noqa: F401  测试按 turn.httpx 打桩

from .locale import language  # noqa: F401  子模块经 turn.language 取语言
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
from .turn_chain import (  # noqa: E402
    _CHAIN_POSTS,
    _chain_request,
    _chain_start,
    _language,
    _post_chain,
    _refusal_text,
    on_session_end,
    on_session_reset,
)
from .turn_outbox import (  # noqa: E402
    _client,
    _do_outbox,
    _outbox_loop,
    _poll_outbox,
    _receipt,
    _release_slot,
    _send,
    _server,
    _start_worker,
)
