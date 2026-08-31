"""Small shared helpers used across the pipeline stages."""

import logging
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_config.yaml"

_config_cache = None


def load_config() -> dict:
    """Load pipeline_config.yaml once and cache it for the process."""
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH) as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def project_path(relative_path: str) -> Path:
    """Resolve a config-relative path to an absolute Path under the project root."""
    return PROJECT_ROOT / relative_path


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
