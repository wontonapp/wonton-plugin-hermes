"""出箱轮询：取停链和待发消息，执行完回报结果；停链时清掉会话和轮次槽。"""

import asyncio
import os
from urllib.parse import urlsplit


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


from . import turn
