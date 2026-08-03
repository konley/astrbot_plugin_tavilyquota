"""Smoke test: verify the plugin module can be imported.

This test does NOT require a running AstrBot instance; it monkeypatches
astrbot.api.* to stub objects so the import succeeds offline.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)


def _stub_astrbot_modules():
    """Install minimal stubs so plugin import works without astrbot installed."""
    if "astrbot" in sys.modules:
        return  # assume real astrbot present
    # astrbot.api.event
    api_event = types.ModuleType("astrbot.api.event")

    def _passthrough_decorator(*args, **kwargs):
        def deco(func):
            return func

        return deco

    api_event.filter = types.SimpleNamespace(
        command=_passthrough_decorator,
        regex=_passthrough_decorator,
        on_llm_request=_passthrough_decorator,
        on_decorating_result=_passthrough_decorator,
        permission_type=_passthrough_decorator,
        platform_adapter_type=_passthrough_decorator,
    )
    api_event.AstrMessageEvent = object
    # astrbot.api.provider
    api_provider = types.ModuleType("astrbot.api.provider")
    api_provider.ProviderRequest = type("ProviderRequest", (), {"system_prompt": ""})
    # astrbot.api.star
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = type("Context", (), {})
    api_star.Star = type("Star", (), {"__init__": lambda self, *a, **k: None})

    def _register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    api_star.register = _register
    # astrbot.core.platform.astr_message_message
    core_plat = types.ModuleType("astrbot.core.platform.astr_message_message")
    core_plat.MessageResult = type("MessageResult", (), {})
    # Package hierarchy
    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = type(
        "AstrBotConfig", (), {"get": lambda self, k, d=None: d}
    )
    api_mod.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        critical=lambda *a, **k: None,
    )
    for name, mod in [
        ("astrbot", types.ModuleType("astrbot")),
        ("astrbot.api", api_mod),
        ("astrbot.core", types.ModuleType("astrbot.core")),
        ("astrbot.core.platform", types.ModuleType("astrbot.core.platform")),
        ("astrbot.api.event", api_event),
        ("astrbot.api.provider", api_provider),
        ("astrbot.api.star", api_star),
        ("astrbot.core.platform.astr_message_message", core_plat),
    ]:
        sys.modules.setdefault(name, mod)


def test_import():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    assert hasattr(mod, "TavilyQuota")


def test_class_registered():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    cls = getattr(mod, "TavilyQuota")
    assert callable(cls)


def test_mask_key():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    k = "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"
    masked = mod._mask_key(k)
    assert masked == "tvly-dev-3OY****LcDOur"
    assert k not in masked
    assert mod._mask_key("short") == "****"


def test_remaining_pct():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    assert mod._remaining_pct(0, 1000) == 100.0
    assert mod._remaining_pct(800, 1000) == pytest.approx(20.0)
    assert mod._remaining_pct(1000, 1000) == 0.0
    assert mod._remaining_pct(0, None) is None


def test_light():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    assert mod._light(100.0) == "🟢"
    assert mod._light(50.0) == "🟡"
    assert mod._light(10.0) == "🔴"
    assert mod._light(None) == "⚪"


def test_dedup_keys():
    _stub_astrbot_modules()
    mod = importlib.import_module("main")
    auto = ["tvly-dev-AAA", "tvly-dev-BBB", "  tvly-dev-AAA  "]
    manual = ["tvly-dev-BBB", "tvly-dev-CCC"]
    assert mod._dedup_keys(auto, manual) == [
        "tvly-dev-AAA",
        "tvly-dev-BBB",
        "tvly-dev-CCC",
    ]
