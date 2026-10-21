"""Shared data models passed between modules."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    """Base for config models: unknown keys are errors, so YAML typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class FailureType(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    FORMAT_ERROR = "FORMAT_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


DEFAULT_RETRYABLE = [
    FailureType.TIMEOUT,
    FailureType.CONNECTION_ERROR,
    FailureType.RATE_LIMIT,
    FailureType.SERVER_ERROR,
]

ValidationType = Literal["non_empty", "exact_match", "json", "schema"]




class ModelConfig(Strict):
    name: str = Field(min_length=1)
    base_url: str = Field(default="https://api.openai.com/v1", pattern=r"^https?://")
    api_key_env: str = Field(default="API_KEY", min_length=1)


class RequestConfig(Strict):
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)


class WorkloadConfig(Strict):
    requests: int = Field(gt=0)
    concurrency: int = Field(default=1, ge=1, le=1)
    min_interval_seconds: float = Field(default=0, ge=0)


class RetryConfig(Strict):
    enabled: bool = False
    max_attempts: int = Field(default=1, gt=0)
    backoff_seconds: float = Field(default=1, ge=0)
    max_backoff_seconds: float = Field(default=30, ge=0)
    retry_on: list[FailureType] = Field(default_factory=lambda: list(DEFAULT_RETRYABLE))

    @model_validator(mode="after")
    def _consistent(self):
        if not self.enabled and self.max_attempts != 1:
            raise ValueError("max_attempts must be 1 when retry is disabled")
        return self


class ValidationConfig(Strict):
    type: ValidationType


class ExperimentConfig(Strict):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_\-]+$")
    dataset: str = "datasets/prompts.json"
    model: ModelConfig
    request: RequestConfig
    test: WorkloadConfig
    retry: RetryConfig = Field(default_factory=RetryConfig)
    validation: ValidationConfig




SCHEMA_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class Task(Strict):
    """One evaluation task.

    `expected` depends on `validation`:
      non_empty   -> None (any non-blank text passes)
      exact_match -> the answer string
      json        -> the exact JSON value the response must equal
      schema      -> {field: JSON type name}, e.g. {"name": "string", "age": "integer"}
    """

    id: str = Field(min_length=1)
    category: Literal["simple_text", "structured_output", "classification", "transformation"]
    prompt: str = Field(min_length=1)
    validation: ValidationType
    expected: Any = None

    @model_validator(mode="after")
    def _expected_fits_validation(self):
        v, e = self.validation, self.expected
        ok = {
            "non_empty": e is None,
            "exact_match": isinstance(e, str) and e.strip() != "",
            "json": isinstance(e, (dict, list)),
            "schema": isinstance(e, dict) and bool(e) and all(t in SCHEMA_TYPES for t in e.values()),
        }[v]
        if not ok:
            raise ValueError(f"task {self.id!r}: expected value does not fit validation {v!r}")
        return self


class Dataset(Strict):
    version: str = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)
    sha256: str = ""

    @model_validator(mode="after")
    def _unique_ids(self):
        seen, dupes = set(), set()
        for t in self.tasks:
            (dupes if t.id in seen else seen).add(t.id)
        if dupes:
            raise ValueError(f"duplicate task ids: {sorted(dupes)}")
        return self




class APIResponse(BaseModel):
    """Normalized outcome of one HTTP call, independent of provider."""

    success: bool
    text: str | None = None
    http_status: int | None = None
    latency_seconds: float = Field(ge=0)
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error_type: FailureType | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def _success_xor_error(self):
        if self.success == (self.error_type is not None):
            raise ValueError("success must be True exactly when error_type is None")
        return self


class RequestResult(BaseModel):
    """One attempt at one task: API response plus validation verdict."""

    task_id: str
    attempt_number: int = Field(gt=0)
    started_at: datetime
    finished_at: datetime
    response: APIResponse
    validation_passed: bool | None = None
    validation_message: str | None = None
    failure_type: FailureType | None = None
    retryable: bool = False

    @property
    def success(self) -> bool:
        return self.failure_type is None


class ExperimentResult(BaseModel):
    id: int | None = None
    config: ExperimentConfig
    dataset_version: str
    dataset_hash: str
    app_version: str
    task_limit: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    attempts: list[RequestResult] = Field(default_factory=list)
