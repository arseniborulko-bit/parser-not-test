import importlib
import os
import unittest
from unittest.mock import patch


class ResolveAsinsFromEnvironmentTests(unittest.TestCase):
    def test_env_override_returns_single_asin(self):
        with patch.dict(os.environ, {"SCRAPINGDOG_TOKEN": "test-token", "TEST_ASIN": "https://www.amazon.com/dp/B0CZ767JDG"}, clear=False):
            config = importlib.reload(importlib.import_module("config"))
            self.assertEqual(config.resolve_asins_from_environment(["fallback"]), ["B0CZ767JDG"])


if __name__ == "__main__":
    unittest.main()
