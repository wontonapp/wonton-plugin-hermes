"""会话环境、核心地址和网关 runner：各模块共用的取数。"""

import os
import sys

try:
    import gateway.session_context as _session_context
except Exception:  # 上游老版本没有它：只认环境变量
    _session_context = None


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
    context = sys.modules.get("gateway.session_context") or _session_context
    get = getattr(context, "get_session_env", None)
    return get(name) if get is not None else os.environ.get(name, "")


def _session_id():
    return turn._session_var("HERMES_SESSION_ID")


def _runner():
    if turn._S["runner"] is not None:
        return turn._S["runner"]
    try:
        from gateway.run import _gateway_runner_ref

        turn._S["runner"] = _gateway_runner_ref()
    except Exception as exc:
        turn.logger.warning("拿不到网关：%r", exc)
    return turn._S["runner"]


from . import turn
