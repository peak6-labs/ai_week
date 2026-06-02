import importlib
import pytest


def test_weather_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.weather")
    assert hasattr(mod, "main")


def test_polymarket_price_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.polymarket_price")
    assert hasattr(mod, "main")


def test_polymarket_whale_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.polymarket_whale")
    assert hasattr(mod, "main")


def test_x_pipeline_importable():
    mod = importlib.import_module("kalshi_trader.pipelines.x")
    assert hasattr(mod, "main")
