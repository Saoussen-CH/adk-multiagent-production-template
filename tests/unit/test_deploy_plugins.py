"""build_plugins() gating: Model Armor ADK plugin is opt-in via env."""
import importlib

import pytest


@pytest.fixture()
def deploy_module(monkeypatch):
    # deploy.py loads config at import; provide required env before (re)import.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_STORAGE_BUCKET", "gs://test-bucket")
    import deployment.deploy as deploy
    return deploy


def test_default_is_logging_only(monkeypatch, deploy_module):
    monkeypatch.delenv("MODEL_ARMOR_ADK_PLUGIN", raising=False)
    plugins = deploy_module.build_plugins()
    assert len(plugins) == 1
    assert type(plugins[0]).__name__ == "LoggingPlugin"


def test_enabled_adds_model_armor_plugin(monkeypatch, deploy_module):
    monkeypatch.setenv("MODEL_ARMOR_ADK_PLUGIN", "true")
    monkeypatch.setitem(deploy_module.MODEL_ARMOR_CONFIG, "enabled", True)
    monkeypatch.setitem(
        deploy_module.MODEL_ARMOR_CONFIG, "template_id", "projects/p/locations/l/templates/t"
    )
    plugins = deploy_module.build_plugins()
    names = [type(p).__name__ for p in plugins]
    assert "ModelArmorSafetyFilterPlugin" in names


def test_enabled_but_unconfigured_stays_logging_only(monkeypatch, deploy_module):
    monkeypatch.setenv("MODEL_ARMOR_ADK_PLUGIN", "true")
    monkeypatch.setitem(deploy_module.MODEL_ARMOR_CONFIG, "enabled", False)
    plugins = deploy_module.build_plugins()
    assert len(plugins) == 1
