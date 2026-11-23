"""核心恢复后叫醒被 park 的 MCP 服务器：只碰 park 的，不等连接，MCP 循环没起来时什么都不做。"""

import asyncio
import sys
import unittest
from unittest import mock
from pathlib import Path

from tools import mcp_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wonton import revive_parked_mcp  # noqa: E402


class Loop:
    def __init__(self, running=True):
        self.running = running
        self.calls = []

    def is_running(self):
        return self.running

    def call_soon_threadsafe(self, callback):
        self.calls.append(callback)


class Server:
    def __init__(self, park_reason):
        self._park_reason = park_reason
        self._reconnect_event = asyncio.Event()


class ReviveTest(unittest.TestCase):
    def serve(self, loop, servers):
        for name, value in (("_mcp_loop", loop), ("_servers", servers)):
            patcher = mock.patch.object(mcp_tool, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_parked_servers_are_woken(self):
        loop = Loop()
        parked, healthy = Server("after initial connection failures"), Server(None)
        self.serve(loop, {"wonton": parked, "other": healthy})
        revive_parked_mcp(source={})
        self.assertEqual(len(loop.calls), 1)
        loop.calls[0]()
        self.assertTrue(parked._reconnect_event.is_set())
        self.assertFalse(healthy._reconnect_event.is_set())

    def test_nothing_happens_without_the_running_loop(self):
        loop = Loop(running=False)
        self.serve(loop, {"wonton": Server("parked")})
        revive_parked_mcp(source={})
        self.assertEqual(loop.calls, [])


if __name__ == "__main__":
    unittest.main()
