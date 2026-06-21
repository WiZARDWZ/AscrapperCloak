import unittest
import importlib
import os
import sys
import types
from unittest import mock

sys.modules.setdefault("pyodbc", types.SimpleNamespace(connect=lambda *args, **kwargs: None))

import config
import db_layer
from tools import cloak_smoke_common


class DeploymentConfigTests(unittest.TestCase):
    def test_runtime_summary_exposes_resolved_profile_and_paths(self):
        summary = config.safe_runtime_summary()
        self.assertEqual(summary["runtime_profile"], config.RUNTIME_PROFILE)
        self.assertEqual(summary["runtime_dir"], config.RUNTIME_DIR)
        self.assertEqual(summary["output_dir"], config.OUTPUT_DIR)
        self.assertEqual(summary["headless"], str(config.HEADLESS).lower())

    def test_env_can_disable_resource_blocking(self):
        overrides = {
            "LOW_BANDWIDTH_MODE": "0",
            "BLOCK_HEAVY_RESOURCES": "0",
            "BLOCK_TRACKERS": "0",
            "BLOCK_IMAGES": "0",
            "BLOCK_MEDIA": "0",
            "BLOCK_FONTS": "0",
            "BLOCK_MAPS": "0",
            "BLOCK_ADS": "0",
            "BLOCK_ANALYTICS": "0",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            reloaded = importlib.reload(config)
            try:
                self.assertFalse(reloaded.LOW_BANDWIDTH_MODE)
                self.assertFalse(reloaded.BLOCK_HEAVY_RESOURCES)
                self.assertFalse(reloaded.BLOCK_TRACKERS)
                self.assertFalse(reloaded.BLOCK_IMAGES)
                self.assertFalse(reloaded.BLOCK_MEDIA)
                self.assertFalse(reloaded.BLOCK_FONTS)
                self.assertFalse(reloaded.BLOCK_MAPS)
                self.assertFalse(reloaded.BLOCK_ADS)
                self.assertFalse(reloaded.BLOCK_ANALYTICS)
            finally:
                importlib.reload(config)

    def test_bool_env_accepts_explicit_false_values(self):
        overrides = {
            "BROWSER_USE_RUNTIME_PROFILE_STATE": "0",
            "BROWSER_PAGE_STATE_DEBUG": "off",
            "CLOAK_DISABLE_HTTP2": "false",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            reloaded = importlib.reload(config)
            try:
                self.assertFalse(reloaded.BROWSER_USE_RUNTIME_PROFILE_STATE)
                self.assertFalse(reloaded.BROWSER_PAGE_STATE_DEBUG)
                self.assertFalse(reloaded.CLOAK_DISABLE_HTTP2)
            finally:
                importlib.reload(config)


    def test_cloak_env_parses_humanize_geoip_viewport_and_http2_default(self):
        overrides = {
            "BROWSER_ENGINE": "cloak",
            "HEADLESS": "0",
            "CLOAK_HUMANIZE": "1",
            "CLOAK_GEOIP": "1",
            "CLOAK_PROXY": "http://user:pass@example.test:8080",
            "CLOAK_VIEWPORT": "1365x768",
            "CLOAK_DISABLE_HTTP2": "0",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            reloaded = importlib.reload(config)
            try:
                self.assertTrue(reloaded.CLOAK_HUMANIZE)
                self.assertTrue(reloaded.CLOAK_GEOIP)
                self.assertEqual(reloaded.CLOAK_PROXY, "http://user:pass@example.test:8080")
                self.assertEqual(reloaded.CLOAK_VIEWPORT_WIDTH, 1365)
                self.assertEqual(reloaded.CLOAK_VIEWPORT_HEIGHT, 768)
                self.assertEqual(reloaded.CLOAK_HTTP2_MODE, "default")
                self.assertFalse(reloaded.CLOAK_DISABLE_HTTP2)
            finally:
                importlib.reload(config)

    def test_invalid_cloak_human_config_json_fails_fast(self):
        with mock.patch.dict(os.environ, {"CLOAK_HUMAN_CONFIG_JSON": "[]"}, clear=False):
            with self.assertRaises(ValueError):
                importlib.reload(config)
        importlib.reload(config)

    def test_effective_profile_respects_chrome_and_cloak(self):
        with mock.patch.object(config, "BROWSER_ENGINE", "cloak"), \
             mock.patch.object(config, "CLOAK_PROFILE_DIR", "cloak-profile"), \
             mock.patch.object(config, "CHROME_PROFILE_DIR", "chrome-profile"), \
             mock.patch.object(config, "MODULE2_PROFILE_BASE_DIR", None):
            self.assertEqual(config.get_effective_browser_profile_dir("module1"), "cloak-profile")
        with mock.patch.object(config, "BROWSER_ENGINE", "chrome"), \
             mock.patch.object(config, "CHROME_PROFILE_DIR", "chrome-profile"):
            self.assertEqual(config.get_effective_browser_profile_dir("module3"), "chrome-profile")

    def test_module2_profile_base_overrides_module2_only(self):
        with mock.patch.object(config, "BROWSER_ENGINE", "cloak"), \
             mock.patch.object(config, "CLOAK_PROFILE_DIR", "cloak-profile"), \
             mock.patch.object(config, "MODULE2_PROFILE_BASE_DIR", "module2-profile"):
            self.assertEqual(config.get_effective_browser_profile_dir("module2"), "module2-profile")
            self.assertEqual(config.get_effective_browser_profile_dir("module1"), "cloak-profile")

    def test_tool_profile_dir_overrides_env_config(self):
        args = types.SimpleNamespace(profile_dir="output/cloak_tests/my_profile", fresh_profile=False)
        with mock.patch.object(config, "CHROME_PROFILE_DIR", "chrome-profile"), \
             mock.patch.object(config, "CLOAK_PROFILE_DIR", "cloak-profile"), \
             mock.patch.object(config, "MODULE2_PROFILE_BASE_DIR", "module2-profile"), \
             mock.patch.object(config, "BROWSER_USE_RUNTIME_PROFILE_STATE", True):
            effective = cloak_smoke_common.apply_profile_dir(cloak_smoke_common.resolve_profile_dir(args, "output/cloak_tests", "x"), module2=True)
            self.assertTrue(effective.endswith(os.path.join("output", "cloak_tests", "my_profile")))
            self.assertEqual(config.CHROME_PROFILE_DIR, effective)
            self.assertEqual(config.CLOAK_PROFILE_DIR, effective)
            self.assertEqual(config.MODULE2_PROFILE_BASE_DIR, effective)
            self.assertFalse(config.BROWSER_USE_RUNTIME_PROFILE_STATE)

    def test_sqlserver_connection_string_uses_db_env_shape(self):
        patches = [
            mock.patch.object(config, "DB_DRIVER", "ODBC Driver 18 for SQL Server"),
            mock.patch.object(config, "DB_HOST", "127.0.0.1"),
            mock.patch.object(config, "DB_PORT", "1433"),
            mock.patch.object(config, "DB_NAME", "AScrapperProd"),
            mock.patch.object(config, "DB_USER", "ascrapper_user"),
            mock.patch.object(config, "DB_PASSWORD", "secret-password"),
            mock.patch.object(config, "DB_ENCRYPT", "yes"),
            mock.patch.object(config, "DB_TRUST_SERVER_CERTIFICATE", "yes"),
            mock.patch.object(config, "DB_TIMEOUT", 30),
            mock.patch.object(config, "DB_TRUSTED_CONNECTION", "no"),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
            conn_str = config.build_sqlserver_connection_string(include_password=False)

        self.assertIn("DRIVER={ODBC Driver 18 for SQL Server}", conn_str)
        self.assertIn("SERVER=127.0.0.1,1433", conn_str)
        self.assertIn("DATABASE=AScrapperProd", conn_str)
        self.assertIn("UID=ascrapper_user", conn_str)
        self.assertIn("PWD=***", conn_str)
        self.assertIn("Encrypt=yes", conn_str)
        self.assertIn("TrustServerCertificate=yes", conn_str)
        self.assertIn("Connection Timeout=30", conn_str)

    def test_db_layer_connect_uses_configured_timeout_and_no_sqlite_fallback(self):
        with mock.patch.object(config, "build_sqlserver_connection_string", return_value="DRIVER={ODBC Driver 18 for SQL Server};SERVER=host;DATABASE=db;"), \
             mock.patch.object(config, "DB_TIMEOUT", 45), \
             mock.patch.object(db_layer.pyodbc, "connect", return_value=object()) as connect_mock:
            result = db_layer.connect()

        self.assertIsNotNone(result)
        connect_mock.assert_called_once_with(
            "DRIVER={ODBC Driver 18 for SQL Server};SERVER=host;DATABASE=db;",
            autocommit=False,
            timeout=45,
        )


if __name__ == "__main__":
    unittest.main()
