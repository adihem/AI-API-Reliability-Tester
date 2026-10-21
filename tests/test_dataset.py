import json
from collections import Counter
from pathlib import Path

import pytest

from tester.dataset import DatasetError, load_dataset

DATASET = Path(__file__).parent.parent / "datasets" / "prompts.json"


def task(id="t1", **over):
    return {"id": id, "category": "classification", "prompt": "p", "validation": "exact_match",
            "expected": "BUG", **over}


def write(tmp_path, data):
    p = tmp_path / "d.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def test_shipped_dataset():
    ds = load_dataset(DATASET)
    assert len(ds.tasks) == 30
    counts = Counter(t.category for t in ds.tasks)
    assert counts == {"simple_text": 10, "structured_output": 10, "classification": 5, "transformation": 5}
    assert len(ds.sha256) == 64


def test_hash_tracks_content(tmp_path):
    a = load_dataset(write(tmp_path, {"version": "1", "tasks": [task()]})).sha256
    b = load_dataset(write(tmp_path, {"version": "1", "tasks": [task(prompt="q")]})).sha256
    assert a != b


def test_duplicate_ids_rejected(tmp_path):
    with pytest.raises(DatasetError, match="duplicate task ids: \\['t1'\\]"):
        load_dataset(write(tmp_path, {"version": "1", "tasks": [task(), task(), task("t2")]}))


@pytest.mark.parametrize(
    "over",
    [
        {"validation": "fuzzy"},
        {"validation": "exact_match", "expected": None},
        {"validation": "exact_match", "expected": "  "},
        {"validation": "json", "expected": "BUG"},
        {"validation": "schema", "expected": {"a": "str"}},
        {"validation": "schema", "expected": {}},
        {"validation": "non_empty", "expected": "x"},
        {"category": "poetry"},
        {"prompt": ""},
        {"extra_field": 1},
    ],
)
def test_bad_task_rejected(tmp_path, over):
    with pytest.raises(DatasetError):
        load_dataset(write(tmp_path, {"version": "1", "tasks": [task(**over)]}))


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        [task()],
        {"version": "1", "tasks": []},
        {"tasks": [task()]},
        {"version": "1", "tasks": [task()], "sha256": "x"},
    ],
)
def test_bad_file_rejected(tmp_path, data):
    with pytest.raises(DatasetError):
        load_dataset(write(tmp_path, data))


def test_missing_file(tmp_path):
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "nope.json")
