"""按这一轮的说话人重取工具清单，再把新清单交给这一轮。"""

import contextlib
import sys


def _refresh_tools():
    """在 MCP 循环上把每个在线服务的清单按当前身份重取一遍。

    服务端那侧的 ``tools/list`` 拿的是 ``_S["last"]``：``_run_on_mcp_loop`` 拷的是循环线程的 context，工具
    线程里 set 的 ``_CURRENT`` 到不了。只连着读，不重连（不是 ``reprobe_tool_availability``）。全都失败时抛错，
    一个都没连上时返回 False——那样不记说话人，下一轮再来，连接自己的第一次清单会带这一轮的帽子。
    """
    from tools.mcp_tool import _servers
    from tools.mcp_tool_loop import _run_on_mcp_loop

    tasks = [task for task in _servers.values() if task.session is not None]
    if not tasks:
        return False
    errors = []
    for server_task in tasks:
        try:
            _run_on_mcp_loop(lambda task=server_task: task._refresh_tools(), timeout=turn.REFRESH_TIMEOUT)
        except Exception as exc:
            errors.append(exc)
    if len(errors) == len(tasks):
        raise errors[0]
    for error in errors:
        turn.logger.warning("有一个 MCP 服务的清单没重取上：%r", error)
    return True


def _refresh_agent_tools(agent):
    """把注册表里现在的工具重新交给这一轮。

    ``preserve_prefix`` 只往后面加、不动模型已经见过的顺序；这样重取到的写工具这一轮就能用，不用等下一轮。
    """
    from tools.mcp_tool_agent import refresh_agent_mcp_tools

    return refresh_agent_mcp_tools(agent, quiet_mode=True, preserve_prefix=True)


def _agent_for_turn(session_id):
    """这一轮正在跑的 Agent；工具清单挂在它身上。

    ``pre_llm_call`` 没有这个参数，上面几层调用方有（``_collect_pre_llm_call_context(agent, …)``）。上游把
    ``pre_llm_call`` 扔在 ``hermes-hook-*`` 线程上跑（``plugins_dispatch`` 的 bounded 集合），调用点的栈帧到不了
    钩子线程，所以先按栈找（同线程的调用点），再按会话号去网关的 agent 缓存里找。只认会话号对得上的那一个，
    找不到就 None，新清单从下一轮起生效。
    """
    frame = sys._getframe(1)
    while frame is not None:
        agent = frame.f_locals.get("agent")
        if agent is not None and getattr(agent, "session_id", None) == session_id:
            return agent
        frame = frame.f_back
    cache = getattr(turn._S["gateway"], "_agent_cache", None)
    if not cache:
        return None
    lock = getattr(turn._S["gateway"], "_agent_cache_lock", None)
    with (lock or contextlib.suppress()):
        for entry in list(cache.values()):
            agent = entry[0] if isinstance(entry, tuple) else entry
            if getattr(agent, "session_id", None) == session_id:
                return agent
    return None


def _remember_for_listing(session_id, sender):
    """按这一轮的说话人重取工具清单，并把身份留给 ``tools/list``。

    核心按代表头过滤广告出去的清单：共享 Bot 的清单按能用人里最低的那位封顶，写工具会被整片摘掉，而这份
    清单只在连接、重连时取一次，挂着不动的连接会一直用连接那一刻的帽子。模型调用之前按这一轮的身份重取
    一遍，模型这一轮才看得见他买得起的工具。同一个说话人不重取。
    """
    execution = turn._cron_execution()
    if execution is not None:
        identity = turn._cron_identity(execution)
    elif sender:
        identity = {"sender": sender, "session": session_id, "room": None, "thread": None}
    else:
        identity = None
    turn._S["last"] = identity
    if identity is None or identity["sender"] == turn._S["listed"]:
        return
    try:
        refreshed = turn._refresh_tools()
    except Exception as exc:
        turn.logger.warning("按 %s 重取工具清单失败，这一轮按旧清单走：%r", identity["sender"], exc)
        return
    if not refreshed:
        return
    if turn._S["last"] is not identity:
        return  # 重取期间这一轮已经换人或没了，别把旧身份记成已取
    turn._S["listed"] = identity["sender"]
    agent = turn._agent_for_turn(session_id)
    if agent is None:
        return
    try:
        added = turn._refresh_agent_tools(agent)
    except Exception as exc:
        turn.logger.warning("把重取到的清单交给这一轮失败：%r", exc)
        return
    if added:
        turn.logger.warning("按 %s 重取后清单多了：%s", identity["sender"], ", ".join(sorted(added)))


from . import turn
