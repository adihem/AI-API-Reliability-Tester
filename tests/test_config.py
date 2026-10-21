from pathlib import Path

import pytest
import yaml

from tester.config import ConfigError, load_config
from tester.models import FailureType

CONFIG_DIR = Path(__file__).parent.parent / "config"

VALID = {
    "name": "t",
    "model": {"name": "m"},
    "request": {"temperature": 0.2, "max_tokens": 10, "timeout_seconds": 5},
    "test": {"requests": 3},
    "validation": {"type": "exact_match"},
}


def write(tmp_path, data):
    p = tmp_path / "c.yaml"
    p.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return p


def with_change(section, key, value):
    data = {k: dict(v) if isinstance(v, dict) else v for k, v in VALID.items()}
    data.setdefault(section, {})[key] = value
    return data


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_shipped_configs_are_valid(path):
    cfg = load_config(path)
    assert cfg.name == path.stem


def test_valid_config_and_defaults(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert cfg.test.concurrency == 1
    assert cfg.retry.enabled is False and cfg.retry.max_attempts == 1
    assert FailureType.TIMEOUT in cfg.retry.retry_on
    assert FailureType.AUTHENTICATION_ERROR not in cfg.retry.retry_on


def test_retry_config_parsed(tmp_path):
    data = {**VALID, "retry": {"enabled": True, "max_attempts": 3, "retry_on": ["TIMEOUT"]}}
    cfg = load_config(write(tmp_path, data))
    assert cfg.retry.max_attempts == 3
    assert cfg.retry.retry_on == [FailureType.TIMEOUT]


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("test", "requests", 0),
        ("request", "timeout_seconds", -5),
        ("retry", "max_attempts", 0),
        ("test", "concurrency", -1),
        ("test", "concurrency", 2),  # sequential runner only, for now
        ("request", "temperature", 3),
        ("request", "max_tokens", 0),
        ("validation", "type", "fuzzy"),
        ("retry", "retry_on", ["NOT_A_FAILURE"]),
        ("retry", "max_attempts", 3),  # retry disabled but >1 attempts
        ("request", "temprature", 0.5),  # typo -> unknown key
    ],
)
def test_invalid_values_rejected(tmp_path, section, key, value):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, with_change(section, key, value)))


@pytest.mark.parametrize("missing", ["name", "model", "request", "test", "validation"])
def test_missing_section_rejected(tmp_path, missing):
    data = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(write(tmp_path, data))


def test_bad_name_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, {**VALID, "name": "../escape"}))


@pytest.mark.parametrize("text", ["name: [unclosed", "", "- just\n- a list\n"])
def test_unparsable_or_non_mapping_yaml(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
