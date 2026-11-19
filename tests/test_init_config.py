"""init 修配置：旧包写的模型网关配置在重建容器时撤掉，主人自己选的供应商不动。"""

import sys
import unittest
from pathlib import Path

from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parents[1]))

import init_config  # noqa: E402

ROOT = Path(__file__).parents[1]
OLD_PACK = """\
model:
  provider: custom:magpie
  default: ${WONTON_MODEL_DEFAULT}
providers:
  magpie:
    api: ${WONTON_MODEL_BASE_URL}
    key_env: WONTON_MODEL_API_KEY
"""


def load(text):
    return YAML().load(text) or {}


class InitConfigCase(unittest.TestCase):
    def setUp(self):
        self.seed = load((ROOT / "config.yaml").read_text())

    def test_old_gateway_model_is_dropped(self):
        config = load(OLD_PACK)
        self.assertTrue(init_config.apply(config, self.seed))
        self.assertNotIn("model", config)
        self.assertNotIn("providers", config)

    def test_owner_chosen_provider_is_kept(self):
        config = load(
            "model:\n  provider: openrouter\n  default: vendor/model\n"
            "providers:\n  magpie:\n    api: http://192.0.2.10/v1\n  mine:\n    api: http://192.0.2.10/v1\n"
        )
        init_config.apply(config, self.seed)
        self.assertEqual(config["model"]["provider"], "openrouter")
        self.assertEqual(list(config["providers"]), ["magpie", "mine"])

    def test_pack_seed_has_no_model(self):
        # 包里的初始配置不写 model：新装的 Bot 由主人在 Hermes 网页里选。
        self.assertNotIn("model", self.seed)
        self.assertNotIn("providers", self.seed)


if __name__ == "__main__":
    unittest.main()
