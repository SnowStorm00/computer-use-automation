from __future__ import annotations

import asyncio
import json

import pytest

from cua.artifact.schema import CapabilityArtifact
from cua.config import settings
from cua.runtime import ensure_http, load_artifact, run_replay_artifact
from cua.safety.policy import load_policy
from cua.safety.redact import redact_obj, redact_text
from cua.session import STORE


@pytest.fixture(scope="session", autouse=True)
async def _http():
    await ensure_http()


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return load_artifact(str(settings.evidence_dir / "lookup_member_savings.json"))


def test_redact_ssn_and_password():
    text = redact_text("TIN 000-00-4412 password: demo")
    assert "[SSN]" in text
    assert "demo" not in text
    obj = redact_obj({"password": "demo", "member_id": "12345", "tin": "000-00-4412"})
    assert obj["password"] == "[SECRET]"
    assert obj["tin"] == "[SECRET]"
    assert obj["member_id"] == "12345"


def test_policy_blocks_foreign_origin():
    policy = load_policy(settings.policy_path)
    assert policy.origin_allowed("http://127.0.0.1:8080/bank/login")
    assert not policy.origin_allowed("https://evil.example/bank")
    assert policy.action_allowed("click")
    assert not policy.action_allowed("evaluate_js")


def test_artifact_roundtrip(artifact: CapabilityArtifact):
    raw = artifact.model_dump_json(by_alias=True)
    again = CapabilityArtifact.model_validate_json(raw)
    assert again.id == "lookup_member_savings"
    assert again.parameters[0].name == "member_id"
    assert any(r.code == "MEMBER_NOT_FOUND" for r in again.exception_rules)
    dumped = json.loads(raw)
    assert dumped["exception_rules"][0]["class"] == "business_outcome"
    assert "demo" not in raw
    assert "{{secrets.operator_password}}" in raw


@pytest.mark.asyncio
async def test_replay_success(artifact: CapabilityArtifact):
    result = await run_replay_artifact(artifact, {"member_id": "12345"}, headless=True)
    assert result.status == "success"
    assert "4,250.00" in result.outputs.get("savings_balance", "")


@pytest.mark.asyncio
async def test_replay_member_not_found(artifact: CapabilityArtifact):
    result = await run_replay_artifact(artifact, {"member_id": "99999"}, headless=True)
    assert result.status == "business_outcome"
    assert result.outcome_code == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_replay_access_denied(artifact: CapabilityArtifact):
    result = await run_replay_artifact(artifact, {"member_id": "11111"}, headless=True)
    assert result.status == "business_outcome"
    assert result.outcome_code == "ACCESS_DENIED"


@pytest.mark.asyncio
async def test_hitl_pause_and_resume(artifact: CapabilityArtifact):
    async def resume_when_human():
        for _ in range(200):
            sess = STORE.latest()
            if sess and sess.control == "human":
                snap = sess.snapshot()
                assert snap["intervention"]["reason"]
                assert snap["goal"]
                sess.resume()
                return
            await asyncio.sleep(0.1)
        raise AssertionError("never paused for a human")

    helper = asyncio.create_task(resume_when_human())
    result = await run_replay_artifact(artifact, {"member_id": "00000"}, headless=True)
    await helper
    assert result.status in {"failed", "escalated", "success", "business_outcome"}
    assert helper.done()
