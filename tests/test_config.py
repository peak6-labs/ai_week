import os, importlib, pytest


def test_config_loads_env(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import kalshi_trader.config as cfg
    importlib.reload(cfg)
    assert cfg.KALSHI_ENV == "demo"
    assert cfg.ANTHROPIC_API_KEY == "test-key"


def test_base_url_demo(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    import kalshi_trader.config as cfg
    importlib.reload(cfg)
    assert "demo" in cfg.KALSHI_BASE_URL


def test_base_url_prod(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    import kalshi_trader.config as cfg
    importlib.reload(cfg)
    assert "demo" not in cfg.KALSHI_BASE_URL
