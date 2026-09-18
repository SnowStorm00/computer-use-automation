from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import uvicorn

from cua.api import ReplayIn, create_app
from cua.artifact.schema import CapabilityArtifact, RunResult
from cua.config import settings
from cua.agent.loop import DiscoverAgent, DiscoverResult
from cua.evidence import RunLog
from cua.replay.engine import ReplayEngine
from cua.safety.policy import load_policy
from cua.session import STORE, LiveSession
from cua.surface.web import WebSurface

_server: uvicorn.Server | None = None
_server_task: asyncio.Task | None = None


async def ensure_http() -> None:
    global _server, _server_task
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.base_url}/health", timeout=0.4)
            if r.status_code == 200:
                return
    except Exception:
        pass
    if _server_task and not _server_task.done():
        return
    app = create_app()
    config = uvicorn.Config(
        app,
        host=settings.cua_host,
        port=settings.cua_port,
        log_level="warning",
        loop="asyncio",
    )
    _server = uvicorn.Server(config)
    _server_task = asyncio.create_task(_server.serve())
    for _ in range(80):
        if _server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("server failed to start")


def serve_forever() -> None:
    app = create_app()
    uvicorn.run(app, host=settings.cua_host, port=settings.cua_port, log_level="info")


def secrets() -> dict[str, str]:
    return {
        "operator_password": settings.cua_operator_password,
        "operator_id": settings.cua_operator_id,
    }


async def run_discover(
    goal: str,
    entry_url: str | None = None,
    allow_risky: bool = False,
    headless: bool | None = None,
) -> DiscoverResult:
    await ensure_http()
    run_id = uuid.uuid4().hex[:10]
    session_id = uuid.uuid4().hex[:8]
    log = RunLog(settings.runs_dir / "discover" / run_id)
    policy = load_policy(settings.policy_path)
    surface = await WebSurface.launch(policy, headless=settings.cua_headless if headless is None else headless)
    session = LiveSession(session_id, surface, run_id, goal=goal)
    STORE.put(session)
    agent = DiscoverAgent(surface, policy, log, session, secrets(), allow_risky=allow_risky)
    try:
        result = await agent.run(goal, entry_url or settings.bank_url + "/login", run_id)
        if result.artifact:
            settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = settings.artifacts_dir / f"{result.artifact.id}.json"
            path.write_text(result.artifact.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
            log.write_json("artifact.json", json.loads(result.artifact.model_dump_json(by_alias=True)))
        return result
    finally:
        await surface.close()
        STORE.drop(session_id)


async def run_replay_artifact(
    artifact: CapabilityArtifact,
    params: dict,
    allow_risky: bool = False,
    tenant_id: str | None = None,
    headless: bool | None = None,
    run_dir: Path | None = None,
) -> RunResult:
    await ensure_http()
    run_id = uuid.uuid4().hex[:10]
    session_id = uuid.uuid4().hex[:8]
    log = RunLog(run_dir or (settings.runs_dir / "replay" / run_id))
    policy = load_policy(settings.policy_path)
    surface = await WebSurface.launch(policy, headless=settings.cua_headless if headless is None else headless)
    session = LiveSession(session_id, surface, run_id, goal=artifact.name)
    STORE.put(session)
    engine = ReplayEngine(
        surface,
        policy,
        log,
        session,
        secrets=secrets(),
        allow_risky=allow_risky or settings.cua_allow_risky,
        tenant_id=tenant_id,
    )
    try:
        result = await engine.run(artifact, params, run_id)
        log.write_json("result.json", json.loads(result.model_dump_json()))
        return result
    finally:
        if session.control != "human":
            await surface.close()
            STORE.drop(session_id)


def load_artifact(path_or_id: str) -> CapabilityArtifact:
    path = Path(path_or_id)
    if not path.exists():
        alt = settings.artifacts_dir / f"{path_or_id}.json"
        if alt.exists():
            path = alt
        else:
            evidence = settings.evidence_dir / "lookup_member_savings.json"
            if path_or_id in {"lookup_member_savings", "lookup-member-savings"} and evidence.exists():
                path = evidence
            else:
                raise FileNotFoundError(path_or_id)
    return CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))


async def run_replay(body: ReplayIn) -> RunResult:
    if body.artifact_path:
        artifact = load_artifact(body.artifact_path)
    elif body.artifact_id:
        artifact = load_artifact(body.artifact_id)
    else:
        raise ValueError("artifact_id or artifact_path required")
    return await run_replay_artifact(artifact, body.params, allow_risky=body.allow_risky, tenant_id=body.tenant_id)
