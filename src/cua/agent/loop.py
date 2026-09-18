from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from cua.agent.prompts import SYSTEM, user_prompt
from cua.artifact.schema import (
    AppRef,
    CapabilityArtifact,
    Checkpoint,
    FrameRef,
    LocatorCandidate,
    OutputField,
    Parameter,
    Step,
    Target,
    now_iso,
)
from cua.config import settings
from cua.evidence import RunLog
from cua.replay.exceptions import default_exception_rules
from cua.safety.policy import Policy, PolicyViolation
from cua.safety.redact import parameterize_secret
from cua.session import LiveSession
from cua.surface.web import WebSurface, describe_locator


class Decision(BaseModel):
    thought: str = ""
    action: Literal["click", "fill", "press", "extract", "wait", "done", "fail", "escalate"]
    frame: Literal["top", "workspace"] | None = None
    role: str | None = None
    name: str | None = None
    name_attr: str | None = None
    text: str | None = None
    extract_as: str | None = None
    press_key: str | None = None
    outputs: dict[str, Any] | None = None
    reason: str | None = None


class DiscoverResult(BaseModel):
    status: str
    run_id: str
    artifact: CapabilityArtifact | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    evidence_dir: str = ""
    session_id: str = ""


class DiscoverAgent:
    def __init__(
        self,
        surface: WebSurface,
        policy: Policy,
        log: RunLog,
        session: LiveSession,
        secrets: dict[str, str],
        allow_risky: bool = False,
    ):
        self.surface = surface
        self.policy = policy
        self.log = log
        self.session = session
        self.secrets = secrets
        self.allow_risky = allow_risky
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.steps: list[Step] = []
        self.history: list[str] = []
        self.outputs: dict[str, Any] = {}

    async def run(self, goal: str, entry_url: str, run_id: str) -> DiscoverResult:
        if not settings.openai_api_key:
            return DiscoverResult(
                status="failed",
                run_id=run_id,
                error="OPENAI_API_KEY is not set",
                evidence_dir=str(self.log.run_dir),
                session_id=self.session.session_id,
            )
        self.log.event("discover_start", goal=goal, entry_url=entry_url)
        await self.surface.goto(entry_url)
        self.steps.append(
            Step(id="s01", kind="navigate", value=entry_url, note="open app")
        )
        for i in range(settings.cua_max_steps):
            self.session.step_index = i
            obs = await self.surface.observe()
            shot = await self.surface.screenshot()
            shot_rel = self.log.save_screenshot(shot, f"step{i}")
            self.log.write_text(f"aria_{i:02d}.txt", obs.aria)
            self.log.event("observe", url=obs.url, title=obs.title, screenshot=shot_rel, dialogs=obs.dialogs)
            decision = await asyncio.to_thread(self._decide, goal, obs, shot, i)
            self.log.event("decide", decision=decision.model_dump())
            self.history.append(f"{decision.action} {decision.name or decision.name_attr or ''} {decision.thought}")
            if decision.action == "done":
                if decision.outputs:
                    self.outputs.update(decision.outputs)
                artifact = self._compile(goal, entry_url, run_id)
                self.log.write_json("artifact.json", json.loads(artifact.model_dump_json(by_alias=True)))
                self.log.event("discover_done", outputs=self.outputs)
                return DiscoverResult(
                    status="success",
                    run_id=run_id,
                    artifact=artifact,
                    outputs=self.outputs,
                    evidence_dir=str(self.log.run_dir),
                    session_id=self.session.session_id,
                )
            if decision.action == "fail":
                return DiscoverResult(
                    status="failed",
                    run_id=run_id,
                    error=decision.reason or "agent failed",
                    evidence_dir=str(self.log.run_dir),
                    session_id=self.session.session_id,
                )
            if decision.action == "escalate":
                await self.session.pause_for_human(
                    decision.reason or "agent requested a human",
                    f"s{i:02d}",
                    shot_rel,
                )
                self.log.event("escalated", reason=decision.reason, screenshot=shot_rel)
                await self.session.wait_for_resume()
                for hs in self.session.human_steps:
                    if hs not in self.steps:
                        self.steps.append(hs)
                self.log.event("resumed_after_human")
                continue
            try:
                await self._act(decision, i)
            except PolicyViolation as exc:
                self.log.event("policy_violation", message=str(exc))
                return DiscoverResult(
                    status="failed",
                    run_id=run_id,
                    error=str(exc),
                    evidence_dir=str(self.log.run_dir),
                    session_id=self.session.session_id,
                )
            except Exception as exc:
                self.log.event("act_error", message=str(exc))
                shot_rel = self.log.save_screenshot(await self.surface.screenshot(), "act_error")
                await self.session.pause_for_human(f"Act failed: {exc}", f"s{i:02d}", shot_rel)
                await self.session.wait_for_resume()
        return DiscoverResult(
            status="failed",
            run_id=run_id,
            error="max steps reached",
            evidence_dir=str(self.log.run_dir),
            session_id=self.session.session_id,
        )

    def _decide(self, goal: str, obs, shot: bytes, step: int) -> Decision:
        import base64

        b64 = base64.b64encode(shot).decode("ascii")
        prompt = user_prompt(goal, obs.url, obs.title, obs.aria[:12000], step, self.history)
        resp = self.client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
                        },
                    ],
                },
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        self.log.write_text(f"llm_{step:02d}.json", raw)
        try:
            return Decision.model_validate_json(raw)
        except Exception:
            try:
                return Decision.model_validate(json.loads(raw))
            except Exception:
                return Decision(action="escalate", reason=f"unparseable model output: {raw[:200]}")

    async def _act(self, decision: Decision, i: int) -> None:
        sid = f"s{i+2:02d}"
        frames = [FrameRef(by="name", value="workspace")] if decision.frame == "workspace" else []
        if decision.action == "wait":
            self.steps.append(Step(id=sid, kind="wait"))
            return
        if decision.action == "press":
            await self.surface.press(decision.press_key or "Enter")
            self.steps.append(Step(id=sid, kind="press", press_key=decision.press_key or "Enter"))
            return
        target = await self._resolve_hint(decision, frames)
        if decision.action == "click":
            if self.policy.is_risky_text(decision.name) and not self.allow_risky:
                raise PolicyViolation(f"risky click blocked: {decision.name}")
            await self.surface.click(target)
            self.steps.append(
                Step(
                    id=sid,
                    kind="click",
                    target=target,
                    risk="irreversible" if self.policy.is_risky_text(decision.name) else "normal",
                )
            )
            return
        if decision.action == "fill":
            raw = decision.text or ""
            value = _expand(raw, self.secrets)
            await self.surface.fill(target, value)
            stored = parameterize_secret(value, self.secrets) or _maybe_param(raw)
            self.steps.append(Step(id=sid, kind="fill", target=target, value=stored))
            return
        if decision.action == "extract":
            text = await self.surface.extract(target)
            name = decision.extract_as or "value"
            if name.lower() in {"tin", "ssn"}:
                text = "[SSN]"
            self.outputs[name] = text
            self.steps.append(Step(id=sid, kind="extract", target=target, extract_as=name))
            return
        raise RuntimeError(f"unhandled action {decision.action}")

    async def _resolve_hint(self, decision: Decision, frames: list[FrameRef]) -> Target:
        locators: list[LocatorCandidate] = []
        if decision.role and decision.name:
            locators.append(
                LocatorCandidate(strategy="role", role=decision.role, value=decision.name, exact=False)
            )
        if decision.name_attr:
            locators.append(LocatorCandidate(strategy="name_attr", value=decision.name_attr))
        if decision.name and decision.action == "extract":
            locators.append(LocatorCandidate(strategy="cell_after_label", value=decision.name))
        if decision.name:
            locators.append(LocatorCandidate(strategy="text", value=decision.name, exact=False))
            locators.append(LocatorCandidate(strategy="label", value=decision.name, exact=False))
        if not locators:
            raise LookupError("model did not provide a target")
        frame_tries = [frames]
        if frames:
            frame_tries.append([])
        else:
            frame_tries.append([FrameRef(by="name", value="workspace")])
        last_err: Exception | None = None
        for try_frames in frame_tries:
            hint = Target(
                description=decision.name or decision.name_attr or decision.action,
                frames=try_frames,
                locators=locators,
            )
            try:
                loc = await self.surface.resolve(hint, settings.cua_step_timeout_ms)
                captured = await describe_locator(self.surface.page, loc, try_frames)
                merged = list(captured.locators)
                for extra in locators:
                    if extra.model_dump() not in [c.model_dump() for c in merged]:
                        merged.append(extra)
                captured.locators = merged
                captured.frames = try_frames
                return captured
            except Exception as exc:
                last_err = exc
        raise LookupError(str(last_err) if last_err else "target not found")

    def _compile(self, goal: str, entry_url: str, run_id: str) -> CapabilityArtifact:
        params = _infer_params(self.steps)
        outputs = [
            OutputField(name=k, type="string", sensitive=k.lower() in {"tin", "ssn", "name"})
            for k in self.outputs
            if k != "outcome_code"
        ]
        slug = _slug(goal)
        return CapabilityArtifact(
            id=slug,
            name=_title(goal),
            description=goal,
            version=1,
            approval_state="draft",
            app=AppRef(
                vendor="corelink",
                product="member-servicing",
                surface_kind="web",
                version_range="4.x",
                entry_url=entry_url,
            ),
            parameters=params,
            outputs=outputs,
            steps=self.steps,
            success_checkpoint=Checkpoint(
                kind="text_visible",
                text=_success_text(self.outputs, goal),
            ),
            exception_rules=default_exception_rules(),
            policy_id="default",
            recorded_at=now_iso(),
            recorded_from_run_id=run_id,
        )


def _expand(text: str, secrets: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if key.startswith("secrets."):
            return secrets.get(key.split(".", 1)[1], "")
        return match.group(0)

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, text)


def _maybe_param(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("{{"):
        return stripped
    if re.fullmatch(r"\d{4,}", stripped):
        return "{{member_id}}"
    return stripped


def _infer_params(steps: list[Step]) -> list[Parameter]:
    found: dict[str, Parameter] = {}
    for step in steps:
        if not step.value:
            continue
        for match in re.findall(r"\{\{\s*([^}]+)\s*\}\}", step.value):
            key = match.strip()
            if key.startswith("secrets."):
                name = key.split(".", 1)[1]
                found[name] = Parameter(
                    name=name,
                    source="secret",
                    sensitive=True,
                    description="Injected from the operator vault, never stored on the artifact.",
                )
            else:
                found[key] = Parameter(name=key, source="input", example=None, description=f"Provided per invocation ({key}).")
    if "member_id" not in found:
        for step in steps:
            if step.kind == "fill" and step.value and step.value.isdigit():
                found["member_id"] = Parameter(name="member_id", source="input", example=step.value)
                step.value = "{{member_id}}"
                break
    return list(found.values())


def _slug(goal: str) -> str:
    words = re.findall(r"[a-z0-9]+", goal.lower())[:6]
    return "-".join(words) or f"cap-{uuid.uuid4().hex[:8]}"


def _title(goal: str) -> str:
    text = goal.strip()
    return text[0].upper() + text[1:] if text else "Capability"


def _success_text(outputs: dict[str, Any], goal: str) -> str:
    if "savings_balance" in outputs or "savings" in goal.lower():
        return "Savings Balance"
    if "confirmation" in outputs or "sub-account" in goal.lower():
        return "Sub-Account Opened"
    return "Member Record"
