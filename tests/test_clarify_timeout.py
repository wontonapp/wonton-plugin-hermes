"""init_config.py 补 agent.clarify_timeout：上游 v0.23.x 把默认从 3600 秒改成 90 秒，
云吞是异步聊天，老 Bot 缺这项时补回升级前的 1 小时；主人自己写过的值不动。"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))

from init_config import apply  # noqa: E402


def applies(config, seed):
    with mock.patch.dict(os.environ, {}, clear=True):
        return apply(config, seed)


class ClarifyTimeoutCase(unittest.TestCase):
    def test_missing_key_follows_pack_value(self):
        config = {"plugins": {"enabled": ["wonton"]}}
        seed = {"agent": {"clarify_timeout": 3600}}
        self.assertTrue(applies(config, seed))
        self.assertEqual(config["agent"]["clarify_timeout"], 3600)

    def test_existing_key_is_owner_value(self):
        config = {"agent": {"clarify_timeout": 90}}
        seed = {"agent": {"clarify_timeout": 3600}}
        applies(config, seed)
        self.assertEqual(config["agent"]["clarify_timeout"], 90)

    def test_missing_key_without_seed_value_is_untouched(self):
        config = {"plugins": {"enabled": ["wonton"]}}
        applies(config, {})
        self.assertNotIn("agent", config)


if __name__ == "__main__":
    unittest.main()
