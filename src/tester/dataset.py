"""Load and validate the evaluation dataset."""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from tester.models import Dataset


class DatasetError(Exception):
    """Dataset file is missing, unparsable, or fails validation."""


def load_dataset(path: str | Path) -> Dataset:
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise DatasetError(f"Dataset file not found: {path}") from None

    try:
        raw = json.loads(data)
    except json.JSONDecodeError as e:
        raise DatasetError(f"Invalid JSON in {path}: {e}") from None

    if not isinstance(raw, dict) or "sha256" in raw:
        raise DatasetError(f"{path} must be an object with 'version' and 'tasks'")

    try:
        ds = Dataset.model_validate(raw)
    except ValidationError as e:
        raise DatasetError(f"Invalid dataset {path}:\n{e}") from None

    return ds.model_copy(update={"sha256": hashlib.sha256(data).hexdigest()})
