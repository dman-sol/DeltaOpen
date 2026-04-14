import json
import os
from pathlib import Path
from typing import Optional

from app.models import AppConfig, ExchangeConfig

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_cached_config: Optional[AppConfig] = None


def load_config() -> Optional[AppConfig]:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    if not _CONFIG_PATH.exists():
        return None
    try:
        with open(_CONFIG_PATH, "r") as f:
            data = json.load(f)
        _cached_config = AppConfig(
            binance=ExchangeConfig(**data["binance"]),
            bybit=ExchangeConfig(**data["bybit"]),
        )
        return _cached_config
    except Exception:
        return None


def reload_config() -> Optional[AppConfig]:
    global _cached_config
    _cached_config = None
    return load_config()
