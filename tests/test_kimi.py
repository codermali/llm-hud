from __future__ import annotations

import unittest

from llm_hud.providers.kimi import KimiProvider
from tests.support import Environment


class KimiProviderTests(unittest.TestCase):
    def test_detected_kimi_uses_builtin_toolbar_without_writes(self):
        with Environment(LLM_HUD_KIMI_BIN="1"):
            provider = KimiProvider()
            result = provider.install("ignored")
            configured, detail = provider.configured()

            self.assertEqual(result.status, "builtin")
            self.assertTrue(configured)
            self.assertIn("built-in toolbar", detail)
            self.assertEqual(provider.capabilities.integration, "builtin")
            self.assertEqual(provider.capabilities.on_demand_metrics, ("quota",))
            self.assertEqual(provider.uninstall().status, "skipped")

    def test_missing_kimi_is_not_reported_as_configured(self):
        with Environment(LLM_HUD_KIMI_BIN=""):
            provider = KimiProvider()
            self.assertEqual(provider.install("ignored").status, "error")
            self.assertFalse(provider.configured()[0])

    def test_external_rendering_is_not_claimed(self):
        provider = KimiProvider()
        with self.assertRaises(NotImplementedError):
            provider.render(b"{}")


if __name__ == "__main__":
    unittest.main()
