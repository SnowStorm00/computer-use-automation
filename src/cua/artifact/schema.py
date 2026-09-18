from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


LocatorStrategy = Literal[
    "role",
    "label",
    "placeholder",
    "name_attr",
    "text",
    "cell_after_label",
    "css",
]


class FrameRef(BaseModel):
    by: Literal["name", "url_substr", "title"] = "name"
    value: str


class LocatorCandidate(BaseModel):
    strategy: LocatorStrategy
    value: str
    role: str | None = None
    exact: bool = False
    nth: int | None = None


class Target(BaseModel):
    description: str
    frames: list[FrameRef] = Field(default_factory=list)
    locators: list[LocatorCandidate]


class Checkpoint(BaseModel):
    kind: Literal["url_matches", "text_visible", "element_visible", "all"] = "text_visible"
    url_pattern: str | None = None
    text: str | None = None
    target: Target | None = None


class ExceptionRule(BaseModel):
    code: str
    class_: Literal["business_outcome", "recoverable", "hard_failure"] = Field(
        alias="class", serialization_alias="class"
    )
    detect: Checkpoint
    action: Literal["return", "dismiss_and_retry", "wait_and_retry", "escalate", "fail"]
    dismiss_target: Target | None = None
    max_retries: int = 2
    message: str = ""

    model_config = {"populate_by_name": True}


class Step(BaseModel):
    id: str
    kind: Literal["navigate", "click", "fill", "press", "extract", "wait", "dismiss_dialog", "human"]
    target: Target | None = None
    value: str | None = None
    press_key: str | None = None
    extract_as: str | None = None
    timeout_ms: int = 15000
    risk: Literal["normal", "irreversible"] = "normal"
    note: str | None = None


class Parameter(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    required: bool = True
    description: str = ""
    example: str | None = None
    source: Literal["input", "secret"] = "input"
    sensitive: bool = False


class OutputField(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    description: str = ""
    sensitive: bool = False


class AppRef(BaseModel):
    vendor: str
    product: str
    surface_kind: Literal["web", "desktop"] = "web"
    version_range: str = "*"
    entry_url: str


class TenantOverlay(BaseModel):
    tenant_id: str
    entry_url: str | None = None
    locator_rewrites: dict[str, list[LocatorCandidate]] = Field(default_factory=dict)
    extra_exception_rules: list[ExceptionRule] = Field(default_factory=list)
    notes: str = ""


class CapabilityArtifact(BaseModel):
    schema_version: str = "1.0.0"
    id: str
    name: str
    description: str
    version: int = 1
    approval_state: Literal["draft", "approved"] = "draft"
    app: AppRef
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step]
    success_checkpoint: Checkpoint
    exception_rules: list[ExceptionRule] = Field(default_factory=list)
    overlays: list[TenantOverlay] = Field(default_factory=list)
    policy_id: str = "default"
    recorded_at: str = ""
    recorded_from_run_id: str = ""

    def overlay_for(self, tenant_id: str | None) -> TenantOverlay | None:
        if not tenant_id:
            return None
        for overlay in self.overlays:
            if overlay.tenant_id == tenant_id:
                return overlay
        return None


class StepError(BaseModel):
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    message: str


class RunResult(BaseModel):
    status: Literal["success", "business_outcome", "failed", "escalated"]
    outcome_code: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: StepError | None = None
    run_id: str
    artifact_id: str | None = None
    evidence_dir: str | None = None
    control: Literal["agent", "human", "paused"] = "agent"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
