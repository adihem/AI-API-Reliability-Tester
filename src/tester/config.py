"""Load and validate experiment YAML files."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from tester.models import ExperimentConfig


class ConfigError(Exception):
    """Config file is missing, unparsable, or fails validation."""


def load_config(path: str | Path) -> ExperimentConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from None

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    try:
        return ExperimentConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid config {path}:\n{e}") from None
