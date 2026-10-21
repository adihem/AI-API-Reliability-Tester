from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tester.models import APIResponse, FailureType, RequestResult, Task


def test_api_response_success():
    r = APIResponse(success=True, text="BUG", http_status=200, latency_seconds=0.4, total_tokens=12)
    assert r.error_type is None


def test_api_response_failure():
    r = APIResponse(success=False, latency_seconds=20, error_type="TIMEOUT", error_message="read timeout")
    assert r.error_type is FailureType.TIMEOUT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"success": True, "error_type": "TIMEOUT"},
        {"success": False},
    ],
)
def test_api_response_inconsistent(kwargs):
    with pytest.raises(ValidationError):
        APIResponse(latency_seconds=1, **kwargs)


def test_api_response_negative_latency():
    with pytest.raises(ValidationError):
        APIResponse(success=True, latency_seconds=-1)


def test_task_json_expected():
    t = Task(
        id="json_001",
        category="structured_output",
        prompt="Return name and age as JSON. Anna is 21.",
        validation="json",
        expected={"name": "Anna", "age": 21},
    )
    assert t.expected["age"] == 21


def test_task_rejects_unknown_category():
    with pytest.raises(ValidationError):
        Task(id="x", category="poetry", prompt="p", validation="exact_match")


def test_request_result_success_flag():
    now = datetime.now(timezone.utc)
    resp = APIResponse(success=False, latency_seconds=20, error_type="TIMEOUT")
    r = RequestResult(
        task_id="t1", attempt_number=1, started_at=now, finished_at=now,
        response=resp, failure_type=resp.error_type, retryable=True,
    )
    assert not r.success
    ok = r.model_copy(update={"failure_type": None, "validation_passed": True})
    assert ok.success
