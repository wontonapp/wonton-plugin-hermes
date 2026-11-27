"""报链开始和结束；核心的拒绝类别换成说话人看得懂的一句话。"""

import asyncio
import concurrent.futures
import threading

from .texts import CHAIN_DENIED, CHAIN_LIMIT, CHAIN_LOOP, CHAIN_OTHER, CHAIN_SELF


_CHAIN_POSTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="wonton-chain")


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


from . import turn
