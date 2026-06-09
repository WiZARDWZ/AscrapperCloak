import sys
import types
from unittest import mock

import cloak_browser_helper as cbh


class FakePage:
    url = "about:blank"

    def on(self, *_args, **_kwargs):
        pass

    def route(self, *_args, **_kwargs):
        pass

    def set_default_navigation_timeout(self, *_args, **_kwargs):
        pass

    def set_default_timeout(self, *_args, **_kwargs):
        pass

    def title(self):
        return ""

    def content(self):
        return ""

    def evaluate(self, *_args, **_kwargs):
        return ""

    def query_selector_all(self, *_args, **_kwargs):
        return []


class FakeContext:
    def __init__(self):
        self._page = FakePage()

    @property
    def pages(self):
        return [self._page]

    def new_page(self):
        return self._page

    def close(self):
        pass


def _install_fake_cloak(monkeypatch, calls):
    def launch_persistent_context(profile_dir, **kwargs):
        calls.append((profile_dir, kwargs))
        return FakeContext()

    module = types.SimpleNamespace(launch_persistent_context=launch_persistent_context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", module)


def test_cloak_humanize_geoip_proxy_kwargs_and_masked_log(monkeypatch, tmp_path, capsys):
    calls = []
    _install_fake_cloak(monkeypatch, calls)
    monkeypatch.setattr(cbh.config, "CLOAK_HUMANIZE", True)
    monkeypatch.setattr(cbh.config, "CLOAK_GEOIP", True)
    monkeypatch.setattr(cbh.config, "CLOAK_PROXY", "http://user:secret@proxy.example.com:8080")
    monkeypatch.setattr(cbh.config, "CLOAK_HUMAN_PRESET", "default")
    monkeypatch.setattr(cbh.config, "CLOAK_HUMAN_CONFIG", {"move": True})
    monkeypatch.setattr(cbh.config, "CLOAK_LOCALE", "en-AU")
    monkeypatch.setattr(cbh.config, "CLOAK_TIMEZONE", "Australia/Sydney")
    monkeypatch.setattr(cbh.config, "CLOAK_HTTP2_MODE", "default")
    cbh.build_cloak_driver(profile_dir_override=str(tmp_path / "profile"))

    kwargs = calls[0][1]
    assert kwargs["humanize"] is True
    assert kwargs["geoip"] is True
    assert kwargs["proxy"] == "http://user:secret@proxy.example.com:8080"
    assert kwargs["human_preset"] == "default"
    assert kwargs["human_config"] == {"move": True}
    assert "locale" not in kwargs and "timezone" not in kwargs
    captured = capsys.readouterr().out
    assert "proxy_configured=True" in captured
    assert "proxy.example.com" not in captured
    assert "secret" not in captured
    assert "user" not in captured
    assert "pr***om:8080" in captured


def test_cloak_args_match_raw_defaults_without_fixed_seed_or_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_SEED", None)
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_STORAGE_QUOTA", None)
    monkeypatch.setattr(cbh.config, "CLOAK_HTTP2_MODE", "default")
    monkeypatch.setattr(cbh.config, "CLOAK_VIEWPORT_WIDTH", 1365)
    monkeypatch.setattr(cbh.config, "CLOAK_VIEWPORT_HEIGHT", 768)
    args = cbh._cloak_args(str(tmp_path / "profile"))
    assert "--fingerprint=42069" not in args
    assert not any(arg.startswith("--fingerprint=") for arg in args)
    assert not any(arg.startswith("--fingerprint-storage-quota=") for arg in args)
    assert "--disable-http2" not in args
    assert "--fingerprint-platform=windows" in args
    assert "--fingerprint-screen-width=1365" in args
    assert "--fingerprint-screen-height=768" in args


def test_explicit_fingerprint_seed_and_storage_quota_are_passed(monkeypatch, tmp_path):
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_SEED", "12345")
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_STORAGE_QUOTA", "9000")
    monkeypatch.setattr(cbh.config, "CLOAK_HTTP2_MODE", "default")
    args = cbh._cloak_args(str(tmp_path / "profile"))
    assert "--fingerprint=12345" in args
    assert "--fingerprint-storage-quota=9000" in args


def test_http2_disable_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_SEED", None)
    monkeypatch.setattr(cbh.config, "CLOAK_FINGERPRINT_STORAGE_QUOTA", None)
    monkeypatch.setattr(cbh.config, "CLOAK_HTTP2_MODE", "default")
    assert "--disable-http2" not in cbh._cloak_args(str(tmp_path / "p1"))
    monkeypatch.setattr(cbh.config, "CLOAK_HTTP2_MODE", "disable")
    assert "--disable-http2" in cbh._cloak_args(str(tmp_path / "p2"))


def test_unsupported_configured_launch_kwarg_fails_clearly(monkeypatch, tmp_path):
    def launch_persistent_context(profile_dir, headless=False, args=None, viewport=None):
        return FakeContext()

    monkeypatch.setitem(sys.modules, "cloakbrowser", types.SimpleNamespace(launch_persistent_context=launch_persistent_context))
    monkeypatch.setattr(cbh.config, "CLOAK_HUMANIZE", True)
    monkeypatch.setattr(cbh.config, "CLOAK_GEOIP", False)
    monkeypatch.setattr(cbh.config, "CLOAK_PROXY", None)
    monkeypatch.setattr(cbh.config, "CLOAK_HUMAN_PRESET", None)
    monkeypatch.setattr(cbh.config, "CLOAK_HUMAN_CONFIG", None)
    monkeypatch.setattr(cbh.config, "CLOAK_LOCALE", "")
    monkeypatch.setattr(cbh.config, "CLOAK_TIMEZONE", "")
    with mock.patch("builtins.print"):
        try:
            cbh.build_cloak_driver(profile_dir_override=str(tmp_path / "profile"))
        except RuntimeError as exc:
            assert "does not support configured option" in str(exc)
            assert "humanize" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")


def test_project_helper_tool_imports_without_telegram_or_sql():
    import tools.test_cloak_project_helper_rea as helper_tool

    assert callable(helper_tool.main)
