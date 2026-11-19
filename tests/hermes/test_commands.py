"""命令菜单：表来自上游、说明按主人语言、写进房间状态事件、表没变不重写、没权限时提示一次。

假的 Matrix 客户端只记状态和发送，网关和真实 Hermes 插件加载照旧：菜单必须来自上游注册表，不能是本文档里的清单。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent import i18n, secret_scope
from gateway.config import Platform
from hermes_cli.plugins import PluginManager, PluginManifest
from hermes_constants import pin_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
from mautrix.errors import MForbidden, MNotFound
from tools import skills_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wonton import commands  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ROOM = "!dm:example.org"


class FakeClient:
    def __init__(self):
        self.mxid = "@bot:example.org"
        self.state = {}
        self.writes = []
        self.notices = []
        self.forbidden = False

    async def get_joined_rooms(self):
        return [ROOM]

    async def get_state_event(self, room, event_type, state_key=""):
        if self.forbidden:
            raise MForbidden("no")
        try:
            return self.state[(room, str(event_type), state_key)]
        except KeyError:
            raise MNotFound("not found") from None

    async def send_state_event(self, room, event_type, content, state_key="", **_):
        if self.forbidden:
            raise MForbidden("no")
        self.state[(room, str(event_type), state_key)] = content
        self.writes.append((room, str(event_type), state_key, content))
        return "$state"

    async def send_message_event(self, room, event_type, content, **_):
        self.notices.append((room, content["body"]))
        return "$notice"


class CommandsCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        shutil.copytree(ROOT / "wonton", self.home / "plugins/wonton")
        self.home.joinpath("config.yaml").write_text("plugins:\n  enabled: [wonton]\n", encoding="utf-8")
        self.home.joinpath("skills/demo").mkdir(parents=True)
        skills_dir = mock.patch.object(skills_tool, "SKILLS_DIR", self.home / "skills")
        skills_dir.start()
        self.addCleanup(skills_dir.stop)
        self.home.joinpath("skills/demo/SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo skill\n---\n\n说明。\n", encoding="utf-8"
        )
        environment = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_LANGUAGE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        pin_process_hermes_home(self.home)
        self.addCleanup(pin_process_hermes_home, None)
        token = set_hermes_home_override(str(self.home))
        self.addCleanup(reset_hermes_home_override, token)
        scope = secret_scope.set_secret_scope(None)
        self.addCleanup(secret_scope.reset_secret_scope, scope)
        i18n.reset_language_cache()
        self.addCleanup(i18n.reset_language_cache)
        self.manager = PluginManager(scope_key=str(self.home))
        self.addCleanup(self.manager.unload)
        manifest = PluginManifest(name="wonton", source="user", path=str(self.home / "plugins/wonton"))
        self.manager._load_plugin(manifest)
        self.assertIsNone(self.manager._plugins["wonton"].error)

        self.client = FakeClient()
        adapter = types.SimpleNamespace(_client=self.client)
        commands._S.update(
            runner=types.SimpleNamespace(adapters={Platform.MATRIX: adapter}),
            loop=asyncio.get_running_loop(),
            worker=None,
            inflight=set(),
            written={},
            refused=set(),
            table=None,
            warned=False,
        )
        language = mock.patch.object(commands.locale, "language", mock.AsyncMock(return_value="ja"))
        self.language = language.start()
        self.addCleanup(language.stop)

    async def test_table_comes_from_the_upstream_registry(self):
        content, _digest = commands._build("ja")
        names = [item["name"] for item in content["commands"]]
        # 网关能用的内置命令在里面，只在 CLI 能用的不在。
        self.assertIn("model", names)
        self.assertNotIn("clear", names)
        # 技能命令也在里面，来自 Hermes 自己的技能目录。
        self.assertIn("demo", names)
        self.assertNotIn("slash.", json.dumps(content, ensure_ascii=False))
        model = next(item for item in content["commands"] if item["name"] == "model")
        start = next(item for item in content["commands"] if item["name"] == "start")
        prune = next(item for item in content["commands"] if item["name"] == "autoprune")
        # 说明按主人语言；上游没翻的退英文原文。
        self.assertEqual(start["description"], i18n.t("slash.start.description", lang="ja"))
        self.assertNotEqual(start["description"], "Accept Telegram start pings without a reply")
        self.assertEqual(model["description"], i18n.t("slash.model.description", lang="ja"))
        self.assertEqual(prune["description"], "Turn automatic pruning of old sessions on or off")
        self.assertTrue(model["hint"].startswith("[model|reset]"))

    async def test_writes_once_and_skips_an_unchanged_table(self):
        await commands._sync(ROOM)
        self.assertEqual(len(self.client.writes), 1)
        room, event_type, key, content = self.client.writes[0]
        self.assertEqual(event_type, "app.wonton.bot.commands")
        self.assertEqual(key, self.client.mxid)
        self.assertTrue(content["commands"])
        await commands._sync(ROOM)
        self.assertEqual(len(self.client.writes), 1)
        # 重启后：房间里已有的内容一样也不重写。
        commands._S["written"] = {}
        commands._S["table"] = None
        await commands._sync(ROOM)
        self.assertEqual(len(self.client.writes), 1)

    async def test_language_change_rewrites(self):
        await commands._sync(ROOM)
        self.language.return_value = "en"
        await commands._sync(ROOM)
        self.assertEqual(len(self.client.writes), 2)
        before, after = (write[3] for write in self.client.writes)
        self.assertNotEqual(before, after)

    async def test_no_permission_says_it_once(self):
        self.client.forbidden = True
        await commands._sync(ROOM)
        await commands._sync(ROOM)
        self.assertEqual(len(self.client.notices), 1)
        self.assertEqual(self.client.notices[0][0], ROOM)
        self.assertEqual(self.client.notices[0][1], commands.REFUSED_BODY["ja"])

    async def test_sweep_writes_every_joined_room(self):
        await commands._sync_all()
        self.assertEqual([write[0] for write in self.client.writes], [ROOM])


class NormalizeCase(unittest.TestCase):
    def test_drops_bad_rows_and_counts_the_overflow(self):
        rows = [("/a", "带斜杠", ""), ("dup", "第一次", ""), ("dup", "第二次", ""), ("x" * 65, "太长", "")]
        rows += [(f"c{index:03d}", "说明", "") for index in range(205)]
        content, omitted = commands._normalize(rows)
        names = [item["name"] for item in content["commands"]]
        self.assertEqual(names[0], "a")
        self.assertEqual(names.count("dup"), 1)
        self.assertNotIn("x" * 65, names)
        self.assertEqual(len(names), commands.MAX_COMMANDS)
        self.assertEqual(omitted, 7)

    def test_keeps_the_body_under_the_byte_limit(self):
        rows = [(f"c{index:03d}", "长" * 400, "") for index in range(commands.MAX_COMMANDS)]
        content, omitted = commands._normalize(rows)
        self.assertLessEqual(len(commands._dump(content)), commands.MAX_BYTES)
        self.assertGreater(omitted, 0)
        self.assertTrue(content["commands"])
        self.assertLessEqual(len(content["commands"][0]["description"]), commands.MAX_DESCRIPTION_CHARS)


if __name__ == "__main__":
    unittest.main()
