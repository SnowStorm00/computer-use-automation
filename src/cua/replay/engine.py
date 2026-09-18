from __future__ import annotations

import asyncio
import re
from typing import Any

from cua.artifact.schema import (
    CapabilityArtifact,
    Checkpoint,
    ExceptionRule,
    RunResult,
    Step,
    StepError,
    Target,
    TenantOverlay,
)
from cua.evidence import RunLog
from cua.safety.policy import Policy, PolicyViolation
from cua.safety.redact import redact_text
from cua.session import LiveSession
from cua.surface.web import WebSurface


class ReplayEngine:
    def __init__(
        self,
        surface: WebSurface,
        policy: Policy,
        log: RunLog,
        session: LiveSession,
        secrets: dict[str, str] | None = None,
        allow_risky: bool = False,
        tenant_id: str | None = None,
    ):
        self.surface = surface
        self.policy = policy
        self.log = log
        self.session = session
        self.secrets = secrets or {}
        self.allow_risky = allow_risky
        self.tenant_id = tenant_id
        self._skip_codes: set[str] = set()

    async def run(self, artifact: CapabilityArtifact, params: dict[str, Any], run_id: str) -> RunResult:
        overlay = artifact.overlay_for(self.tenant_id)
        rules = list(artifact.exception_rules)
        if overlay:
            rules.extend(overlay.extra_exception_rules)
            if overlay.entry_url:
                artifact = artifact.model_copy(
                    update={"app": artifact.app.model_copy(update={"entry_url": overlay.entry_url})}
                )
        self.log.event("replay_start", artifact_id=artifact.id, params=_public_params(params, artifact))
        outputs: dict[str, Any] = {}
        try:
            for i, step in enumerate(artifact.steps):
                self.session.step_index = i
                step = _apply_overlay(step, overlay)
                hit = await self._scan_exceptions(rules, step.id)
                if hit:
                    result = await self._handle_exception(hit, step.id, artifact.id, run_id, outputs)
                    if result:
                        return result
                result = await self._exec(step, params, artifact, outputs)
                if result:
                    return result
                hit = await self._scan_exceptions(rules, step.id)
                if hit:
                    result = await self._handle_exception(hit, step.id, artifact.id, run_id, outputs)
                    if result:
                        return result
            ok, detail = await self._checkpoint(artifact.success_checkpoint)
            if not ok:
                shot = await self._shot("checkpoint_failed")
                self.log.event("checkpoint_failed", detail=detail, screenshot=shot)
                return RunResult(
                    status="failed",
                    run_id=run_id,
                    artifact_id=artifact.id,
                    evidence_dir=str(self.log.run_dir),
                    error=StepError(
                        step_id="checkpoint",
                        expected=_checkpoint_expected(artifact.success_checkpoint),
                        observed=redact_text(await self.surface.page_text())[:500],
                        message=detail,
                    ),
                )
            self.log.event("replay_success", outputs=outputs)
            return RunResult(
                status="success",
                run_id=run_id,
                artifact_id=artifact.id,
                outputs=outputs,
                evidence_dir=str(self.log.run_dir),
            )
        except PolicyViolation as exc:
            self.log.event("policy_violation", message=str(exc))
            return RunResult(
                status="failed",
                run_id=run_id,
                artifact_id=artifact.id,
                evidence_dir=str(self.log.run_dir),
                error=StepError(message=str(exc)),
            )
        except Exception as exc:
            shot = await self._shot("crash")
            self.log.event("crash", message=str(exc), screenshot=shot)
            return RunResult(
                status="failed",
                run_id=run_id,
                artifact_id=artifact.id,
                evidence_dir=str(self.log.run_dir),
                error=StepError(
                    step_id=artifact.steps[self.session.step_index].id if artifact.steps else None,
                    message=str(exc),
                    observed=redact_text(await self.surface.page_text())[:500],
                ),
            )

    async def _exec(
        self,
        step: Step,
        params: dict[str, Any],
        artifact: CapabilityArtifact,
        outputs: dict[str, Any],
    ) -> RunResult | None:
        if step.risk == "irreversible" and not (
            self.allow_risky or artifact.approval_state == "approved"
        ):
            shot = await self._shot("risky_blocked")
            await self.session.pause_for_human(
                f"Irreversible step {step.id} needs approval or a human.",
                step.id,
                shot,
            )
            self.log.event("escalated", reason="risky_step", step_id=step.id, screenshot=shot)
            await self.session.wait_for_resume()
            self.log.event("resumed_after_human", human_steps=[s.model_dump() for s in self.session.human_steps])
            return None

        self.log.event("step", step=step.model_dump())
        target = _render_target(step.target, params, self.secrets) if step.target else None
        if step.kind == "navigate":
            url = _render(step.value or artifact.app.entry_url, params, self.secrets)
            if not self.policy.origin_allowed(url):
                raise PolicyViolation(f"navigate blocked: {url}")
            await self.surface.goto(url)
        elif step.kind == "click":
            if not target:
                raise RuntimeError(f"{step.id} click missing target")
            await self.surface.click(target, step.timeout_ms)
        elif step.kind == "fill":
            if not target:
                raise RuntimeError(f"{step.id} fill missing target")
            value = _render(step.value or "", params, self.secrets)
            await self.surface.fill(target, value, step.timeout_ms)
        elif step.kind == "press":
            await self.surface.press(step.press_key or "Enter")
        elif step.kind == "extract":
            if not target or not step.extract_as:
                raise RuntimeError(f"{step.id} extract missing target/name")
            outputs[step.extract_as] = await self.surface.extract(target, step.timeout_ms)
        elif step.kind == "wait":
            await asyncio.sleep(0.4)
            if target:
                await self.surface.resolve(target, step.timeout_ms)
        elif step.kind == "dismiss_dialog":
            if target:
                await self.surface.click(target, step.timeout_ms)
        elif step.kind == "human":
            pass
        else:
            raise RuntimeError(f"unknown step kind {step.kind}")
        shot = await self._shot(step.id)
        self.log.event("step_done", step_id=step.id, screenshot=shot, url=self.surface.page.url)
        return None

    async def _scan_exceptions(self, rules: list[ExceptionRule], step_id: str) -> ExceptionRule | None:
        for rule in rules:
            if rule.code in self._skip_codes:
                continue
            if await self._checkpoint_bool(rule.detect):
                self.log.event("exception_detected", code=rule.code, class_=rule.class_, step_id=step_id)
                return rule
        return None

    async def _handle_exception(
        self,
        rule: ExceptionRule,
        step_id: str,
        artifact_id: str,
        run_id: str,
        outputs: dict[str, Any],
    ) -> RunResult | None:
        if rule.action == "return" or rule.class_ == "business_outcome":
            return RunResult(
                status="business_outcome",
                outcome_code=rule.code,
                run_id=run_id,
                artifact_id=artifact_id,
                outputs=outputs,
                evidence_dir=str(self.log.run_dir),
                error=StepError(step_id=step_id, message=rule.message or rule.code),
            )
        if rule.action == "dismiss_and_retry" and rule.dismiss_target:
            await self.surface.click(rule.dismiss_target, 5000)
            return None
        if rule.action == "wait_and_retry":
            await asyncio.sleep(1.0)
            return None
        if rule.action == "escalate":
            shot = await self._shot("escalate")
            await self.session.pause_for_human(rule.message or rule.code, step_id, shot)
            self.log.event("escalated", reason=rule.code, screenshot=shot)
            await self.session.wait_for_resume()
            self._skip_codes.add(rule.code)
            self.log.event("resumed_after_human")
            return None
        shot = await self._shot("hard_failure")
        return RunResult(
            status="failed",
            outcome_code=rule.code,
            run_id=run_id,
            artifact_id=artifact_id,
            evidence_dir=str(self.log.run_dir),
            error=StepError(
                step_id=step_id,
                message=rule.message or rule.code,
                observed=redact_text(await self.surface.page_text())[:500],
                expected="flow to continue",
            ),
        )

    async def _checkpoint(self, cp: Checkpoint) -> tuple[bool, str]:
        ok = await self._checkpoint_bool(cp)
        if ok:
            return True, "ok"
        return False, f"checkpoint {cp.kind} failed"

    async def _checkpoint_bool(self, cp: Checkpoint) -> bool:
        checks = []
        if cp.kind in {"url_matches", "all"} and cp.url_pattern:
            checks.append(await self.surface.url_matches(cp.url_pattern))
        if cp.kind in {"text_visible", "all"} and cp.text:
            checks.append(await self.surface.visible_text(cp.text, timeout_ms=400))
        if cp.kind in {"element_visible", "all"} and cp.target:
            try:
                await self.surface.resolve(cp.target, 1500)
                checks.append(True)
            except Exception:
                checks.append(False)
        if not checks:
            return False
        if cp.kind == "all":
            return all(checks)
        return any(checks)

    async def _shot(self, label: str) -> str:
        data = await self.surface.screenshot()
        self.session.last_screenshot = data
        return self.log.save_screenshot(data, label)


def _render_target(target: Target, params: dict[str, Any], secrets: dict[str, str]) -> Target:
    locators = []
    for cand in target.locators:
        locators.append(cand.model_copy(update={"value": _render(cand.value, params, secrets)}))
    return target.model_copy(
        update={
            "locators": locators,
            "description": _render(target.description, params, secrets),
        }
    )


def _render(template: str, params: dict[str, Any], secrets: dict[str, str]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key.startswith("secrets."):
            name = key.split(".", 1)[1]
            return secrets.get(name, "")
        val = params.get(key)
        return "" if val is None else str(val)

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, template)


def _public_params(params: dict[str, Any], artifact: CapabilityArtifact) -> dict[str, Any]:
    sensitive = {p.name for p in artifact.parameters if p.sensitive or p.source == "secret"}
    out = {}
    for k, v in params.items():
        out[k] = "[SECRET]" if k in sensitive else v
    return out


def _apply_overlay(step: Step, overlay: TenantOverlay | None) -> Step:
    if not overlay or not step.target:
        return step
    rewrite = overlay.locator_rewrites.get(step.id)
    if not rewrite:
        return step
    target = step.target.model_copy(update={"locators": rewrite})
    return step.model_copy(update={"target": target})


def _checkpoint_expected(cp: Checkpoint) -> str:
    if cp.text:
        return f"visible text: {cp.text}"
    if cp.url_pattern:
        return f"url: {cp.url_pattern}"
    if cp.target:
        return f"element: {cp.target.description}"
    return cp.kind
