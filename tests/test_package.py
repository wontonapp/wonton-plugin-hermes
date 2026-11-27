"""插件包的形状：``wonton/`` 下的每个模块都经 compose 的 configs 放进 Bot 的数据目录。

Hermes 按目录加载插件，漏掉一个文件时 ``__init__`` 导入失败，整个 wonton 插件静默不加载，单元测试看不出来。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageCase(unittest.TestCase):
    def test_every_plugin_module_is_seeded_into_the_bot_data_dir(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for module in sorted((ROOT / "wonton").glob("*.py")):
            with self.subTest(module=module.name):
                self.assertRegex(compose, rf"file: \./wonton/{re.escape(module.name)}\n")
                self.assertRegex(compose, rf"target: /seed/wonton/{re.escape(module.name)}\n")


if __name__ == "__main__":
    unittest.main()
