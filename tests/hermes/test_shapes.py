"""替身和真库对形状：上游的方法签名、返回类型一偏就红。

替身在别的测试里用 ``sys.modules`` 顶掉了真模块，形状偏了单测照样绿；这里把真模块导出来，
签名和替身逐个比。只在固定镜像里跑（CI 的单测就在这个镜像里）。
"""

import asyncio
import importlib
import inspect
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))


# 其他测试用同名假模块顶掉这些上游包；真模块导进来前要连根摘掉，导完放回去。
FAKE_ROOTS = ("mautrix", "tools", "gateway", "hermes_cli", "mcp", "agent")


def _real(name):
    """导真模块：先把同名的假模块（和假包）摘下来，导完再放回去，别的测试不受影响。

    假模块不是包，真 ``gateway.run`` 走到 ``tools.xxx``、``hermes_cli.xxx`` 就断了，
    所以所有假根一起摘。
    """
    names = (name, *FAKE_ROOTS)
    saved = {key: module for key, module in sys.modules.items()
             if any(key == name_ or key.startswith(f"{name_}.") for name_ in names)}
    for key in saved:
        del sys.modules[key]
    try:
        return importlib.import_module(name)
    finally:
        for key in [key for key in sys.modules
                    if any(key == name_ or key.startswith(f"{name_}.") for name_ in names)]:
            del sys.modules[key]
        sys.modules.update(saved)


def _params(callable_):
    return list(inspect.signature(callable_).parameters)


class ShapeCase(unittest.TestCase):
    def test_the_mautrix_client_double_matches_the_real_client(self):
        from tests.test_side_chat import FakeClient

        real = _real("mautrix.client").Client
        for name in ("get_joined_members", "send_state_event", "get_event", "get_state_event"):
            self.assertTrue(callable(getattr(real, name, None)), f"真 Client 没有 {name}")
            self.assertTrue(callable(getattr(FakeClient, name, None)), f"替身没有 {name}")
            kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
            self.assertEqual(
                [p.name for p in inspect.signature(getattr(FakeClient, name)).parameters.values()
                 if p.kind in kinds],
                [p.name for p in inspect.signature(getattr(real, name)).parameters.values()
                 if p.kind in kinds],
                name,
            )

    def test_joined_members_returns_a_dict_of_members(self):
        # 这条钉的是 0.21.1 的真实形状：dict，不是带 .members 的对象；拿到它的代码只能用长度。
        client_module = _real("mautrix.client")
        self.assertEqual("dict", str(inspect.signature(
            client_module.Client.get_joined_members).return_annotation).split("[")[0])

        async def inner():
            async def request(*args, **kwargs):
                return {"joined": {"@owner:example.org": {}, "@bot:example.org": {}}}

            client = client_module.Client("@bot:example.org", base_url="https://hs.example.org")
            client.api.request = request
            return await client.get_joined_members("!dm1:example.org")

        result = asyncio.run(inner())
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 2)
        self.assertFalse(hasattr(result, "members"))

    def test_the_mcp_session_double_matches_the_real_session(self):
        from tests.test_turn import Session

        module = _real("mcp.client.session")
        # 真会话的 _stamp 是实例属性（预连接时是 _preconnect_stamp），替身按同样的（data, opts）收。
        self.assertEqual(_params(Session._stamp)[1:], _params(module._preconnect_stamp))
        self.assertEqual(_params(Session.adopt), _params(module.ClientSession.adopt))
        real = module.ClientSession
        # 替身的 call_tool 要把核心的 meta 和 progress_callback 都收下转给上游。
        real_params = inspect.signature(real.call_tool).parameters
        self.assertIn("meta", real_params)
        self.assertIn("progress_callback", real_params)
        self.assertIn("arguments", real_params)

    def test_the_tool_handler_double_matches_the_real_handler(self):
        from tests.test_turn import _upstream

        real = _real("tools.mcp_tool_handlers")._call_tool_racing_stdio_death
        self.assertEqual(_params(_upstream)[:3], _params(real)[:3])
        self.assertIn("progress_callback", _params(_upstream))

    def test_the_session_env_double_matches_the_real_one(self):
        from tests.test_turn import TurnCase

        real = _real("gateway.session_context").get_session_env
        self.assertEqual(_params(real), ["name", "default"])
        case = types.SimpleNamespace(env={}, vars={})
        fake = TurnCase._context(case).get_session_env
        self.assertEqual(_params(fake), _params(real))

    def test_the_runner_double_matches_the_real_runner(self):
        real = _real("gateway.run").GatewayRunner
        params = inspect.signature(real._interrupt_and_clear_session).parameters
        for name in ("session_key", "source", "interrupt_reason", "invalidation_reason"):
            self.assertIn(name, params)
        self.assertEqual(
            params["release_running_state"].kind, inspect.Parameter.KEYWORD_ONLY
        )
        self.assertIn("run_generation", inspect.signature(real._drop_turn_slot).parameters)

    def test_the_session_store_double_matches_the_real_store(self):
        from tests.test_turn import Store

        real = _real("gateway.session").SessionStore
        for name in ("_generate_session_key", "peek_session_id", "lookup_by_session_id"):
            self.assertTrue(callable(getattr(real, name, None)), name)
            self.assertTrue(callable(getattr(Store, name, None)), name)
            self.assertEqual(_params(getattr(Store, name)), _params(getattr(real, name)), name)

    def test_the_platform_and_config_doubles_match(self):
        from tests.test_turn import Source

        platform = _real("gateway.config").Platform
        self.assertEqual(Source.platform, platform.MATRIX.value)
        self.assertTrue(callable(_real("hermes_cli.config").load_config_readonly))


if __name__ == "__main__":
    unittest.main()
