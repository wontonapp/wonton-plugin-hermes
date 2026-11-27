"""让 MCP 会话的每次请求带上这一轮的身份头：工具调用和 ``tools/list`` 都包上，来源放进 ``_meta``。"""

import contextvars


# 工具处理器在 MCP 循环上跑，身份在处理器外面 set，包着这次调用：仍然是个 contextvar，但在
# 每次调用自己的任务里 set，值跟着任务走，不看进程里最近是哪一轮。
_CURRENT = contextvars.ContextVar("wonton_turn", default=None)


def _install_headers():
    """给 MCP 会话的每次请求带上这一轮的身份头和来源；清单（``tools/list``）另外先包上会话。

    插件注册时 Hermes 还没导 ``tools.mcp_tool_handlers``，第一条消息到时再包；包过以后这里是空的。
    """
    turn._wrap_list_tools()
    try:
        from tools import mcp_tool_handlers
    except Exception as exc:
        turn.logger.debug("包 MCP 工具处理器失败：%r", exc)
        return
    try:
        original = mcp_tool_handlers._call_tool_racing_stdio_death
    except AttributeError as exc:
        if not turn._S["warned"]:
            turn._S["warned"] = True
            turn.logger.warning("上游没有 MCP 工具处理器 hook 点，身份头不会带上：%r", exc)
        return
    if getattr(original, "_wonton_turn", False):
        return

    async def call(server, *args, **kwargs):
        env = turn._session_id()
        who, source = turn._identity(env)
        turn.logger.debug("MCP 调用的身份取自%s：环境的会话 %r，带的 %r", source, env, who)
        execution = turn._cron_execution()
        cron = await turn._cron_identity_async(execution) if execution is not None else None
        if cron is not None:
            who, source = cron, "定时任务"
            turn._S["last"] = who
            turn.logger.debug("MCP 调用来自定时任务：替创建者 %s 说话，不带会话", who["sender"])
        elif execution is not None and who is not None:
            # 定时任务的会话号核心认不出，别拿它去猜别的轮。
            who = {**who, "session": None}
        if who is None:
            if source != "轮次":
                turn.logger.debug("MCP 调用的身份取自%s：环境的会话 %r，带的 %r", source, env, who)
            if env not in turn._S["unknown"]:
                turn._S["unknown"].add(env)
                if env:
                    turn.logger.warning("这一轮的身份取不到（环境的会话 %r）：调用不带代表头和会话", env)
                else:
                    turn.logger.warning("这一轮没有会话号，身份取不到：调用不带代表头和会话")
            # 明确压一个空身份：别让盖章退回去拿上一轮的 last 猜这次调用。
            token = turn._CURRENT.set({})
            try:
                return await original(server, *args, **kwargs)
            finally:
                turn._CURRENT.reset(token)
        turn._S["last"] = who
        turn._patch_session(server.session)
        token = turn._CURRENT.set(who)
        try:
            return await original(server, *args, **kwargs)
        finally:
            turn._CURRENT.reset(token)

    call._wonton_turn = True
    mcp_tool_handlers._call_tool_racing_stdio_death = call


def _wrap_list_tools():
    """清单也带身份：会话一建好、还没有任何调用时就把它包上。

    清单是连接、重连时取的，那时没有进行中的调用，身份用这一轮在 ``_S["last"]`` 上放的那份。包在
    ``list_tools`` 外面，请求进来先包会话（``_stamp`` 在第一个 await 之前同步跑完，补进 ``opts`` 的头一定在
    发出去的请求里）。
    """
    try:
        from mcp.client.session import ClientSession
    except Exception as exc:
        if not turn._S["warned_list"]:
            turn._S["warned_list"] = True
            turn.logger.warning("上游没有会话的 ``list_tools`` 可包，清单不会带身份：%r", exc)
        return
    if getattr(ClientSession.list_tools, "_wonton_list", False):
        return
    original = ClientSession.list_tools

    async def list_tools(self, *args, **kwargs):
        turn._patch_session(self)
        return await original(self, *args, **kwargs)

    list_tools._wonton_list = True
    ClientSession.list_tools = list_tools


def _stamp_turn(session):
    """让会话的每次请求都带这一轮的身份。

    上游把 resolve_param_headers 在 ``adopt()`` 时就包进 ``_stamp`` 闭包，之后再换 ``_resolve_param_headers``
    不起作用；``_stamp`` 每次请求现调，改它才落到线上。``adopt()`` 会换掉 ``_stamp``，包过以后跟着重包。

    身份按序取：工具线程里这次调用 set 的 ``_CURRENT``；取不到用这一轮在 ``_S["last"]`` 上放的
    （``_run_on_mcp_loop`` 拷的是循环线程的 context，``_CURRENT`` 进不了清单）。
    """
    stamp = session._stamp
    if getattr(stamp, "_wonton_stamp", False):
        return

    def with_turn(data, opts):
        stamp(data, opts)
        who = turn._CURRENT.get()
        if who is None:
            who = turn._S["last"]
        if not who:
            return
        headers = opts.setdefault("headers", {})
        if who.get("sender"):
            headers[turn.ON_BEHALF_OF] = who["sender"]
        if who.get("session"):
            headers[turn.SESSION] = who["session"]

    with_turn._wonton_stamp = True
    session._stamp = with_turn


def _patch_session(session):
    if getattr(session, "_wonton_turn", False):
        return
    session._wonton_turn = True
    turn._stamp_turn(session)
    original_adopt = session.adopt

    def adopt(result):
        original_adopt(result)
        turn._stamp_turn(session)

    session.adopt = adopt
    original_call = session.call_tool

    async def call_tool(name, arguments=None, *args, **kwargs):
        who = turn._CURRENT.get()
        if who and "meta" not in kwargs:
            origin = {name: who[key] for key, name in (("room", "room_id"), ("thread", "thread_id")) if who.get(key)}
            if origin:
                # 核心的 origin 要求 room_id；只有会话号时塞空的 {} 反而解析不了，不带。
                kwargs["meta"] = {turn.ORIGIN: origin}
        return await original_call(name, arguments, *args, **kwargs)

    session.call_tool = call_tool


from . import turn
