"""这一轮替谁说话：会话来源、网页打开者、定时任务的创建者。"""

import asyncio


def _origin(session_id):
    found = turn._S["origins"].get(session_id)
    if found is not None:
        # 同一条会话的下一句：记在 pending 里的是这一句的消息和来源，盖掉上一轮的
        # （被打断或重置以后 peek 不到会话 id，pre_gateway_dispatch 只写 pending）。
        fresh = turn._S["pending"].pop(found.get("session_key"), None)
        if fresh is not None:
            found.update(fresh)
        return found
    store = turn._S["session_store"]
    if store is None or not session_id:
        return None
    try:
        entry = store.lookup_by_session_id(session_id)
    except Exception as exc:
        turn.logger.debug("查会话来源失败 %s：%r", session_id, exc)
        return None
    origin = getattr(entry, "origin", None) if entry else None
    if origin is None or not turn._is_matrix(getattr(origin, "platform", None)):
        return None
    try:
        session_key = store._generate_session_key(origin)
    except Exception:
        session_key = None
    found = turn._S["pending"].pop(session_key, None) or {}
    found.update({
        "sender": getattr(origin, "user_id", None),
        "session_key": session_key,
        "room": getattr(origin, "chat_id", None),
        "thread": getattr(origin, "thread_id", None) or None,
    })
    found.setdefault("event_id", None)
    turn._S["origins"][session_id] = found
    return found


def _identity(session_id):
    """这一轮替谁说话，连同这份身份是哪来的。

    按工具调用自己的会话号查这一轮的来源；说话人取不到时不做猜测，带会话号不带代表头，让核心按会话去认
    ——猜错了卡片就挂到别人的链上。会话号也没有时返回 None，调用不带身份（核心的审计里看得到）。

    会话结束以后迟到的调用只剩会话号这一支：核心对已结束的会话没有在跑的轮，会退到“封顶最严的轮”，
    这条会话再开新链之前可能拿到别的说话人，和完全不带会话头一样，不是准的。
    """
    active = turn._S["active"].get(session_id)
    if active is not None:
        return active, "会话环境"
    origin = turn._S["origins"].get(session_id)
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


def note_page():
    """网页控制台连上来了：出箱轮询和 MCP 头只有网关的入站消息会带上，网页不经过它。"""
    try:
        turn._S["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass
    turn._install_headers()
    turn._start_worker()


def attach_gateway(server):
    """记下上游的会话表模块：网页连接关了要按它认人。网关本身在 ``_S["gateway"]``，两回事。"""
    turn._S["server"] = server


def note_peer(session_id, transport):
    """这条连接刚提交（或恢复）了这条会话。谁都记：终端页签最后提交时不该让早先的网页顶上。"""
    if session_id:
        turn._S["webs"][session_id] = transport
    turn._pages().remember(session_id, transport)


def _pages():
    from . import pages

    return pages


def _page_user(transport):
    """这条连接带的打开者；不是网页连接时 None。"""
    return turn._pages().opener_of(transport)


def _warn_pages(session_id, reason):
    """上游形状变了按会话各报一次：同一进程里另一个会话的形状变化还得能看见。"""
    if session_id in turn._S["page_warned"]:
        return None
    turn._S["page_warned"].add(session_id)
    turn.logger.warning("%s，网页连接认不出人（会话 %s）", reason, session_id)
    return None


def _page_sender(session_id):
    """网页这一轮替谁说话：最后提交这条会话、带着打开者的连接。

    连接在控制台进程里、这一轮在网关进程里跑，提交者从盘上读（``pages.remember`` 写的）。盘上记着却没人
    （终端页签最后提交的）就不算人。记下的连接已经不在会话里时（关了、被上游拆掉）改由还剩的活连接里最后
    提交的那条说；没有会话表或一个活连接都没有才退回记录本身。一个带打开者的都没有就不算人，交回上游的
    ``sender_id``。网页会话没有 Matrix 来源，``pre_gateway_dispatch`` 不来；按轮现取，不缓存到会话。
    """
    recorded = turn._pages().recorded(session_id)
    server = turn._S["server"]
    live = None
    if server is None:
        # 没有控制台的进程（定时任务、旧版环境）是常态，不当事故报。
        turn.logger.debug("没有上游的会话表，网页连接认不出人（会话 %s）", session_id)
    else:
        try:
            live = server._session_live_transports(server._sessions.get(session_id))
        except AttributeError as exc:
            turn._warn_pages(session_id, f"上游的会话表换了形状：{exc!r}")
    remembered = turn._S["webs"].get(session_id)
    # 记下的连接已经不在会话里了（上游拆 Fanout 时子连接的 ``_closed`` 不置位，光看标记发现不了）：记录
    # 就是它的话已经过时，改由还剩的活连接里最后提交的那条说。别的控制台进程里刚打完字的记录本进程
    # 不认识它的连接，照旧信记录。
    stale = (remembered is not None and live is not None
             and not any(peer is remembered for peer in live)
             and (recorded is None or turn._page_user(remembered) == recorded.strip()))
    if live and stale:
        for transport in reversed(live):
            sender = turn._page_user(transport)
            if sender:
                return sender
    if recorded is not None:
        return recorded.strip() or None
    if remembered is not None:
        return turn._page_user(remembered)
    return None


def _cron_execution():
    """手上的定时任务执行，没有时 None。

    先问插件上下文；委派出去的子代理里上游特意让它返回 None（``cron/execution_identity.py``），
    这时退回读上游自己那份执行标记：子代理跑在委派时拷来的 context 里，标记还在，它替同一件事干活。
    """
    ctx = turn._S["ctx"]
    execution = ctx.current_cron_execution() if ctx is not None else None
    if execution is not None:
        return execution
    try:
        from cron.execution_identity import _CURRENT as _UPSTREAM_CURRENT

        return _UPSTREAM_CURRENT.get()
    except Exception as exc:
        if not turn._S["warned_marker"]:
            turn._S["warned_marker"] = True
            turn.logger.warning("拿不到上游的定时任务执行标记，子代理那条身份会失去：%r", exc)
        return None


def _warn_cron(job_id, origin, reason):
    """同一个任务的同一份 origin 只提醒一次：主人改了任务的 origin 会再提醒一次。"""
    key = repr(origin)
    if turn._S["cron_warned"].get(job_id) == key:
        return
    if len(turn._S["cron_warned"]) > 256:
        turn._S["cron_warned"].clear()
    turn._S["cron_warned"][job_id] = key
    turn.logger.warning("定时任务 %s 的 origin 用不上（%s），调用不带身份", job_id, reason)


def _cron_identity(execution):
    """定时任务（含它委派出去的子代理）调工具时替这条任务的创建者说话。没有定时任务时 None。

    定时任务跑的时候没有人当场说话，`_identity` 查不到；任务的 ``origin`` 记着建任务那一轮的会话环境
    （上游 ``tools/cronjob_job_args._origin_from_env``），``origin`` 是建任务时从会话环境抄的字符串，
    A 让 Bot 建的任务就按 A 算，模型在工具子进程里改不了它。

    头是插件自报的，核心只核对“这个 Bot 听不听这个人”（和聊天轮一样）：创建者后来不听这个 Bot 了，核心按
    认不出算、只读。平台不是正好 ``matrix``、没有创建者、任务没了都不带身份，那几样主人从日志里看不出，所以
    各提醒一次；没有身份核心按只读算（核心 #832 起），不按 Bot 的归属人，也不再按共享 Bot 的能用人算。

    定时任务没有进行中的轮次，子代理也不是一轮，所以只带代表头，不带 ``wonton-session``。
    """
    job_id = getattr(execution, "job_id", None)
    if not job_id:
        turn._warn_cron("", None, "执行里没有任务 id")
        return None
    # 一次执行里 origin 不会变；主人改了任务，下一次执行的执行号不同，会重新读。
    cache_key = turn._cron_key(job_id, execution)
    cache = turn._S["cron_origins"]
    if cache_key in cache:
        identity = cache[cache_key]
        return dict(identity) if identity else None
    try:
        from cron.jobs import get_job

        job = get_job(job_id)
    except Exception as exc:
        # 读盘失败可能是暂时的，不记住，下一次调用再试。
        turn._warn_cron(job_id, None, f"读任务失败 {exc!r}")
        return None
    origin = (job or {}).get("origin") or {}
    sender = origin.get("user_id")
    if job is None:
        turn._warn_cron(job_id, origin, "任务没了，不带身份（核心按只读算）")
        identity = None
    elif origin.get("platform") != "matrix":
        turn._warn_cron(job_id, origin, f"建任务的平台是 {origin.get('platform')!r}，不带身份（核心按只读算）")
        identity = None
    elif not sender:
        turn._warn_cron(job_id, origin, "origin 里没有创建者，不带身份（核心按只读算）")
        identity = None
    else:
        identity = {"sender": sender, "room": origin.get("chat_id"), "thread": origin.get("thread_id")}
    if len(cache) > 64:
        cache.clear()
    cache[cache_key] = identity
    return dict(identity) if identity else None


def _cron_key(job_id, execution):
    """一次执行一份缓存：换个执行号（主人改过任务）就重新读 origin。"""
    return f"{job_id}\n{getattr(execution, 'execution_id', '')}"


async def _cron_identity_async(execution):
    """[`_cron_identity`] 的异步版：缓存没命中时把 ``get_job`` 的读盘和解析挪到线程里。

    `get_job` 是同步的，会把整份 ``jobs.json`` 读出来解一遍；在事件循环里直接调用会挡住别的会话。
    """
    identity = turn._S["cron_origins"].get(turn._cron_key(getattr(execution, "job_id", None), execution), ())
    if identity != ():
        return dict(identity) if identity else None
    return await asyncio.to_thread(turn._cron_identity, execution)


from . import turn
