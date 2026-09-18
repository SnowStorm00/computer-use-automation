from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from cua.bank.app import create_bank_app
from cua.config import settings
from cua.session import STORE, LiveSession


HERE = Path(__file__).parent
OPERATOR = (HERE / "operator" / "index.html").read_text(encoding="utf-8")


class ReplayIn(BaseModel):
    artifact_id: str | None = None
    artifact_path: str | None = None
    params: dict = Field(default_factory=dict)
    tenant_id: str | None = None
    allow_risky: bool = False


class HumanClick(BaseModel):
    x: float
    y: float


class HumanType(BaseModel):
    text: str


class HumanPress(BaseModel):
    key: str = "Enter"


def create_app() -> FastAPI:
    app = FastAPI(title="CUA")
    app.mount("/bank", create_bank_app())

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/operator", response_class=HTMLResponse)
    def operator():
        return OPERATOR

    @app.get("/api/sessions")
    def sessions():
        latest = STORE.latest()
        return {"latest": None if not latest else latest.snapshot()}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        sess = _sess(session_id)
        return sess.snapshot()

    @app.get("/api/sessions/{session_id}/screenshot")
    async def screenshot(session_id: str):
        sess = _sess(session_id)
        data = sess.last_screenshot or await sess.surface.screenshot()
        sess.last_screenshot = data
        return Response(content=data, media_type="image/png")

    @app.post("/api/sessions/{session_id}/click")
    async def click(session_id: str, body: HumanClick):
        sess = _sess(session_id)
        target = await sess.human_click(body.x, body.y)
        return {"ok": True, "target": None if not target else target.model_dump()}

    @app.post("/api/sessions/{session_id}/type")
    async def type_text(session_id: str, body: HumanType):
        sess = _sess(session_id)
        await sess.human_type(body.text)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/press")
    async def press(session_id: str, body: HumanPress):
        sess = _sess(session_id)
        await sess.human_press(body.key)
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/resume")
    def resume(session_id: str):
        sess = _sess(session_id)
        sess.resume()
        return {"ok": True, "control": sess.control}

    @app.get("/api/artifacts")
    def artifacts():
        settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        items = []
        for path in sorted(settings.artifacts_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items.append(
                    {
                        "id": data.get("id"),
                        "name": data.get("name"),
                        "path": str(path),
                        "parameters": data.get("parameters", []),
                        "outputs": data.get("outputs", []),
                    }
                )
            except Exception:
                continue
        evidence = settings.evidence_dir / "lookup_member_savings.json"
        if evidence.exists() and not any(i["id"] == "lookup_member_savings" for i in items):
            data = json.loads(evidence.read_text(encoding="utf-8"))
            items.append(
                {
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "path": str(evidence),
                    "parameters": data.get("parameters", []),
                    "outputs": data.get("outputs", []),
                }
            )
        return {"artifacts": items}

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str):
        path = settings.artifacts_dir / f"{artifact_id}.json"
        if not path.exists():
            path = settings.evidence_dir / f"{artifact_id}.json"
        if not path.exists():
            raise HTTPException(404, "artifact not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/capabilities/{artifact_id}/invoke")
    async def invoke(artifact_id: str, body: ReplayIn):
        from cua.runtime import run_replay

        body.artifact_id = artifact_id
        result = await run_replay(body)
        return JSONResponse(json.loads(result.model_dump_json()))

    return app


def _sess(session_id: str) -> LiveSession:
    if session_id == "latest":
        sess = STORE.latest()
    else:
        sess = STORE.get(session_id)
    if not sess:
        raise HTTPException(404, "session not found")
    return sess
