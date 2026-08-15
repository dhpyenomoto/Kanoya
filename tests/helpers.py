from functools import lru_cache
from pathlib import Path

from kanoya.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def demo_config() -> Config:
    return load_config(ROOT / "config.json")
