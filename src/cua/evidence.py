from __future__ import annotations

import json
from pathlib import Path

from cua.safety.redact import redact_obj, redact_text


class RunLog:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.shots = run_dir / "screenshots"
        self.events_path = run_dir / "events.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(parents=True, exist_ok=True)
        self.events_path.write_text("", encoding="utf-8")
        self.n_shots = 0

    def event(self, kind: str, **payload) -> None:
        row = {"kind": kind, **redact_obj(payload)}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def save_screenshot(self, data: bytes, label: str) -> str:
        self.n_shots += 1
        name = f"{self.n_shots:02d}_{_safe(label)}.png"
        path = self.shots / name
        path.write_bytes(data)
        return str(path.relative_to(self.run_dir))

    def write_json(self, name: str, obj) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(redact_obj(obj), indent=2), encoding="utf-8")
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.run_dir / name
        path.write_text(redact_text(text), encoding="utf-8")
        return path


def _safe(label: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    return cleaned[:40] or "shot"
