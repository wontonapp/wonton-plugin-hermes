"""干活轨迹：一轮里每调一次工具，聊天里的状态消息就多一项（ADR-0128，格式见插件规范的“Agent 状态消息”）。

只看不改：挂的都是上游的观察钩子，返回 None，不碰工具的参数和结果，不往上下文里加东西，不调模型。

- 一轮第一次调工具（``pre_tool_call``）时往请求所在的房间发一条 MSC4564 状态消息（``m.notice``，
  ``org.fluxchat.agent``），旁边放 ``app.wonton.trace``：这一轮调过的工具，每项 ``id``、``kind``、``title``、
  ``status``，取值照 ACP 的 ``ToolCall``。``post_tool_call`` 把那一项改成完成或失败，上游审批（危险命令）
  在等主人时状态是 ``waiting``，``on_session_end`` 收成 ``done`` 或 ``failed``。没调工具的一轮什么都不发。
- 推进靠 ``m.replace`` 编辑：状态变了立刻编辑，只有清单变了的最多每 3 秒一次，一轮最多 60 次，到了只等最终那次。
- ``kind``、``title`` 用上游 ACP 适配器同一套函数算（``acp_adapter.tools.get_tool_kind``、
  ``agent.display.build_tool_preview``），再过一遍上游日志的脱敏（``agent.redact.redact_sensitive_text``）；
  这几个不是插件接口，``tests/hermes/test_trace_upstream.py`` 钉住它们。私聊里 ``title`` 是一行预览，
  多人房间里只写类别（ADR-0128 第 3 节）。
- 只记 Matrix 来的、核心准了的一轮：来源取 ``turn`` 记下的这一轮（房间、话题、请求事件、说话人），网页控制台、
  定时任务和委派出去的子代理（会话号对不上）都不记。
- 还没结束的状态消息记在 ``$HERMES_HOME/wonton-trace-open.json``：Bot 重启后，网关第一次收到消息时把它们
  编辑成 ``failed`` 加 ``app.wonton.end: uncertain``，不留一条永远在干活的消息。

钩子跑在 Agent 的线程里，发送都调度回网关的事件循环；每一轮只有一个发送协程（``_pump``），按顺序发和改，
状态变化经 ``asyncio.Event`` 叫醒它。发不出去只记日志：轨迹是旁路，不能打断主人的对话。
``pre_tool_call`` 是上游的策略钩子：回调抛错或超时，上游按拦下这次工具调用处理（``plugins_dispatch`` 的
fail-closed），所以这里的钩子只动内存、自己接住所有异常，文件和网络都在网关循环上做。
"""

import asyncio
import json
import logging
import os
import threading
import time

from . import turn
from .locale import language
from .texts import TRACE_BODY, TRACE_KIND, TRACE_RESULT

logger = logging.getLogger(__name__)

MSC_FIELD = "org.fluxchat.agent"
END_FIELD = "app.wonton.end"
TRACE_FIELD = "app.wonton.trace"
# 清单只留最近几项、每项的说明最长多少：一条事件远小于 Matrix 的 64 KiB。
MAX_CALLS = 30
MAX_TITLE = 120
PREVIEW_LEN = 80
# 只有清单变了的编辑间隔和一轮的编辑上限（ADR-0082 第 2 节）。
EDIT_INTERVAL = 3.0
MAX_EDITS = 60
OPEN_FILE = "wonton-trace-open.json"

_LOCK = threading.Lock()
# 记录文件单独一把锁：写文件慢，不能让钩子线程等它。
_FILE_LOCK = threading.Lock()
_S = {
    "runs": {},        # 会话 id -> Run
    "inflight": set(),
    "recovered": False,
}


class Run:
    """一轮的轨迹。字段在钩子线程里改（持 _LOCK），在网关循环上读来发。"""

    def __init__(self, session_id, origin, dm, loop):
        self.session_id = session_id
        self.room = origin["room"]
        self.thread = origin.get("thread")
        self.request = origin.get("event_id")
        self.sender = origin.get("sender")
        self.dm = dm
        self.loop = loop
        self.calls = []        # [{id, kind, title, status}]
        self.steps = 0
        self.state = "working"
        self.end = None        # cancelled
        self.result = None     # failed 时的一句话（按语言的键）
        self.event_id = None
        self.edits = 0
        self.dirty = True
        self.wake = None       # asyncio.Event，在循环上建


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_approval_request", pre_approval_request)
    ctx.register_hook("post_approval_response", post_approval_response)
    ctx.register_hook("on_session_end", on_session_end)


def pre_gateway_dispatch(**_):
    """网关的事件循环第一次可用时，收尾上次没结束的状态消息。"""
    if _S["recovered"]:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    _S["recovered"] = True
    future = asyncio.run_coroutine_threadsafe(_recover(), loop)
    _track(future)
    return None


def _observer(hook):
    """钩子包一层：任何异常都只记日志、返回 None，不让轨迹拦下工具或打断这一轮。"""

    def wrapped(*args, **kwargs):
        try:
            hook(*args, **kwargs)
        except Exception:
            logger.warning("轨迹钩子 %s 出错，这一项不记", hook.__name__, exc_info=True)
        return None

    wrapped.__name__ = hook.__name__
    return wrapped


@_observer
def pre_tool_call(tool_name="", args=None, session_id="", tool_call_id="", **_):
    run = _run_for(session_id)
    if run is None:
        return None
    kind, title = _describe(tool_name, args if isinstance(args, dict) else {}, run.dm)
    call_id = str(tool_call_id or f"call-{run.steps + 1}")
    with _LOCK:
        run.steps += 1
        run.calls.append({"id": call_id, "kind": kind, "title": title, "status": "in_progress"})
        del run.calls[:-MAX_CALLS]
        run.dirty = True
    _poke(run, urgent=False)
    return None


@_observer
def post_tool_call(session_id="", tool_call_id="", status="", **_):
    run = _S["runs"].get(session_id)
    if run is None or not tool_call_id:
        return None
    with _LOCK:
        for call in reversed(run.calls):
            if call["id"] == str(tool_call_id):
                call["status"] = "failed" if status == "error" else "completed"
                run.dirty = True
                break
    _poke(run, urgent=False)
    return None


@_observer
def pre_approval_request(session_id="", **_):
    _set_state(session_id, "waiting")
    return None


@_observer
def post_approval_response(session_id="", **_):
    _set_state(session_id, "working", only_from="waiting")
    return None


@_observer
def on_session_end(session_id="", completed=False, failed=False, interrupted=False, **_):
    with _LOCK:
        run = _S["runs"].pop(session_id, None)
        if run is None:
            return None
        if interrupted:
            run.state, run.end = "failed", "cancelled"
        elif completed and not failed:
            run.state = "done"
        else:
            run.state, run.result = "failed", "failed"
        run.dirty = True
    _poke(run, urgent=True)
    return None


def _set_state(session_id, state, only_from=None):
    run = _S["runs"].get(session_id)
    if run is None:
        return
    with _LOCK:
        if only_from is not None and run.state != only_from:
            return
        if run.state == state:
            return
        run.state = state
        run.dirty = True
    _poke(run, urgent=True)


def _run_for(session_id):
    """这一轮的轨迹；第一次调工具时开，开不了（不是 Matrix 来的、核心没准、网关循环不在）就不记。"""
    if not session_id:
        return None
    run = _S["runs"].get(session_id)
    if run is not None:
        return run
    active = turn._S["active"].get(session_id)
    if not active or not active.get("room"):
        return None
    loop = turn._S["loop"]
    if loop is None or loop.is_closed():
        logger.warning("轨迹不记：网关事件循环不可用 session=%s", session_id)
        return None
    origin = dict(turn._S["origins"].get(session_id) or {})
    origin.update({key: value for key, value in active.items() if value})
    source = origin.get("source")
    dm = getattr(source, "chat_type", None) == "dm"
    with _LOCK:
        run = _S["runs"].get(session_id)
        if run is None:
            run = Run(session_id, origin, dm, loop)
            _S["runs"][session_id] = run
            _track(asyncio.run_coroutine_threadsafe(_pump(run), loop))
    return run


def _describe(tool_name, args, dm):
    """``(kind, title)``：类别和说明照上游 ACP 适配器；多人房间的说明为 None，由 ``_render`` 按语言写成类别。"""
    kind, preview = _upstream(tool_name, args, with_preview=dm)
    if not dm:
        return kind, None
    title = f"{tool_name}: {preview}" if preview else str(tool_name)
    return kind, title[:MAX_TITLE]


def _upstream(tool_name, args, with_preview):
    """上游 ACP 适配器给这次调用的类别，和终端、ACP 显示的同一行参数预览。

    预览本身不脱敏终端命令（0.23.4 实测 ``Authorization: Bearer …`` 原样留着），所以再过一遍上游日志用的
    ``redact_sensitive_text``（``force=True``：不看主人关没关日志脱敏）。
    """
    from acp_adapter.tools import get_tool_kind
    from agent.display import build_tool_preview
    from agent.redact import redact_sensitive_text

    kind = str(get_tool_kind(tool_name) or "other")
    if not with_preview:
        return kind, None
    # 先整行脱敏再截短：截在密钥中间，脱敏的模式就认不出剩下的半截。
    preview = build_tool_preview(tool_name, args, max_len=0)
    if not preview:
        return kind, None
    preview = redact_sensitive_text(preview, force=True)
    return kind, preview if len(preview) <= PREVIEW_LEN else preview[: PREVIEW_LEN - 1] + "…"


def _poke(run, urgent):
    """叫醒发送协程。只有清单变了的不叫醒：它自己隔 3 秒看一次。"""
    if urgent and run.wake is not None and not run.loop.is_closed():
        run.loop.call_soon_threadsafe(run.wake.set)


def _track(future):
    _S["inflight"].add(future)
    future.add_done_callback(_S["inflight"].discard)


async def _drain():
    """等所有已调度的发送结束（测试用）。"""
    while _S["inflight"]:
        await asyncio.wait({asyncio.wrap_future(f) for f in set(_S["inflight"])})


async def _pump(run):
    run.wake = asyncio.Event()
    try:
        lang = await language(run.sender)
    except Exception as exc:
        logger.debug("取语言失败，轨迹按英文写：%r", exc)
        lang = "en"
    while True:
        # 先清再读：读完之后才来的叫醒不会丢。
        run.wake.clear()
        with _LOCK:
            content = _render(run, lang) if run.dirty else None
            run.dirty = False
            final = run.state in ("done", "failed")
        if content is not None and (final or run.edits < MAX_EDITS - 1 or run.event_id is None):
            await _write(run, content, final)
        if final:
            return
        # 编辑次数快到上限时只等最终那次。
        timeout = None if run.edits >= MAX_EDITS - 1 else EDIT_INTERVAL
        try:
            await asyncio.wait_for(run.wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass


def _render(run, lang):
    agent = {"v": 1, "state": run.state, "steps": run.steps}
    if run.request:
        agent["run"] = run.request
    last = run.calls[-1]["kind"] if run.calls else "other"
    agent["step"] = _kind_text(last, lang)
    if run.result:
        agent["result"] = TRACE_RESULT[run.result][lang]
    calls = [
        {**call, "title": call["title"] or _kind_text(call["kind"], lang)}
        for call in run.calls
    ]
    content = {
        "msgtype": "m.notice",
        "body": _body(run, lang, agent["step"]),
        MSC_FIELD: agent,
        TRACE_FIELD: {"v": 1, "calls": calls},
    }
    if run.end:
        content[END_FIELD] = run.end
    if run.thread:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": run.thread}
    return content


def _kind_text(kind, lang):
    return (TRACE_KIND.get(kind) or TRACE_KIND["other"])[lang]


def _body(run, lang, step):
    if run.end:
        return TRACE_BODY[run.end][lang]
    if run.state == "working":
        return TRACE_BODY["working"][lang].format(step=step, n=run.steps)
    return TRACE_BODY[run.state][lang].format(n=run.steps)


async def _write(run, content, final):
    adapter = _adapter()
    if adapter is None or getattr(adapter, "_client", None) is None:
        logger.warning("轨迹发不出去：Matrix 客户端不在 room=%s", run.room)
        return
    try:
        if run.event_id is None:
            run.event_id = await adapter._send_room_message(run.room, content)
        else:
            await adapter._send_room_message(run.room, _edit(run.event_id, content))
            run.edits += 1
    except Exception as exc:
        logger.warning("轨迹发送失败 room=%s：%r", run.room, exc)
        return
    _remember(run, None if final else content)


def _edit(event_id, content):
    """``m.replace`` 编辑：``m.new_content`` 是全量；话题关系留在外层，新内容里不带关系。"""
    new_content = {key: value for key, value in content.items() if key != "m.relates_to"}
    return {
        **new_content,
        "body": f"* {content['body']}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
    }


def _adapter():
    runner = turn._runner()
    if runner is None:
        return None
    try:
        from gateway.config import Platform

        return runner.adapters.get(Platform.MATRIX)
    except Exception as exc:
        logger.warning("拿不到 Matrix 适配器：%r", exc)
        return None


def _open_path():
    home = os.environ.get("HERMES_HOME")
    return os.path.join(home, OPEN_FILE) if home else None


def _remember(run, content):
    """还在干活的状态消息记下最新内容，结束了删掉：重启后按它收尾。"""
    path = _open_path()
    if path is None or run.event_id is None:
        return
    with _FILE_LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                table = json.load(f)
        except (OSError, ValueError):
            table = {}
        if content is None:
            table.pop(run.event_id, None)
        else:
            table[run.event_id] = {"room": run.room, "content": content, "at": int(time.time())}
        try:
            if not table:
                if os.path.exists(path):
                    os.remove(path)
                return
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(table, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("记不下还没结束的状态消息：%r", exc)


async def _recover():
    """上次没结束的状态消息：清单原样，状态改成 failed 加 uncertain。"""
    path = _open_path()
    if path is None:
        return
    try:
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
    except (OSError, ValueError):
        return
    adapter = _adapter()
    if adapter is None or getattr(adapter, "_client", None) is None:
        logger.warning("收尾不了上次没结束的状态消息：Matrix 客户端不在，下次重启再试")
        return
    lang = await language(None)
    left = {}
    for event_id, entry in table.items():
        content = dict(entry.get("content") or {})
        agent = dict(content.get(MSC_FIELD) or {})
        agent["state"] = "failed"
        agent["result"] = TRACE_RESULT["restarted"][lang]
        content[MSC_FIELD] = agent
        content[END_FIELD] = "uncertain"
        content["body"] = TRACE_BODY["uncertain"][lang]
        try:
            await adapter._send_room_message(entry["room"], _edit(event_id, content))
        except Exception as exc:
            logger.warning("收尾状态消息失败 event=%s：%r", event_id, exc)
            left[event_id] = entry
    try:
        if left:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(left, f, ensure_ascii=False)
        else:
            os.remove(path)
    except OSError as exc:
        logger.warning("改不了还没结束的状态消息记录：%r", exc)
