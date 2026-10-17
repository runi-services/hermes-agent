"""The native prompt-section registration is independent of personality overlays."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _plugin():
    path = Path(__file__).resolve().parents[2] / 'plugins/house-peer-policy/__init__.py'
    spec = importlib.util.spec_from_file_location('peer_policy_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_uses_composed_section_not_personality_or_ephemeral_overlay():
    for personality in ['neutral', 'technical']:
        registered = Mock()
        ctx = SimpleNamespace(
            get_config=lambda key, default='': 'fixture house-only policy',
            register_system_prompt_section=registered,
            personality=personality,
        )
        _plugin().register(ctx)
        registered.assert_called_once_with(
            'a2a.house-work-scope', 'fixture house-only policy',
            position='after_memory', max_chars=2400,
        )


def test_unconfigured_plugin_registers_nothing():
    registered = Mock()
    _plugin().register(SimpleNamespace(get_config=lambda key, default='': default, register_system_prompt_section=registered))
    registered.assert_not_called()
