import os
from pathlib import Path


def default_config_path() -> Path:
    configured = os.environ.get("ROBERT_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "robert" / "config.yml"


def default_data_dir() -> Path:
    configured = os.environ.get("ROBERT_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "robert"
