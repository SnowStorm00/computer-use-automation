from __future__ import annotations

import asyncio
import time
from typing import Literal

from cua.artifact.schema import Step, Target
from cua.surface.web import WebSurface


Control = Literal["agent", "human", "paused"]


class Intervention:
    def __init__(self, reason: str, step_id: str | None, goal: str, screenshot_rel: str | None):
        self.reason = reason
        self.step_id = step_id
        self.goal = goal
        self.screenshot_rel = screenshot_rel
        self.created_at = time.time()


class LiveSession:
    def __init__(self, session_id: str, surface: WebSurface, run_id: str, goal: str = ""):
        self.session_id = session_id
        self.surface = surface
        self.run_id = run_id
        self.goal = goal
        self.control: Control = "agent"
        self.intervention: Intervention | None = None
        self.human_steps: list[Step] = []
        self.resume_event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.step_index = 0
        self.last_screenshot: bytes | None = None
        self.closed = False

    async def pause_for_human(self, reason: str, step_id: str | None, screenshot_rel: str | None) -> None:
        self.control = "human"
        self.intervention = Intervention(reason, step_id, self.goal, screenshot_rel)
        self.resume_event.clear()

    async def wait_for_resume(self) -> None:
        await self.resume_event.wait()
        self.control = "agent"
        self.intervention = None

    def resume(self) -> None:
        if self.control == "human" or self.control == "paused":
            self.control = "agent"
        self.resume_event.set()

    async def human_click(self, x: float, y: float) -> Target | None:
        async with self.lock:
            if self.control != "human":
                raise RuntimeError("session is not under human control")
            target = await self.surface.click_xy(x, y)
            self.human_steps.append(
                Step(
                    id=f"human-{len(self.human_steps)+1}",
                    kind="human",
                    target=target,
                    note=f"click {x:.0f},{y:.0f}",
                )
            )
            self.last_screenshot = await self.surface.screenshot()
            return target

    async def human_type(self, text: str) -> None:
        async with self.lock:
            if self.control != "human":
                raise RuntimeError("session is not under human control")
            await self.surface.type_text(text)
            self.human_steps.append(
                Step(
                    id=f"human-{len(self.human_steps)+1}",
                    kind="human",
                    value="{{redacted}}" if _looks_secret(text) else text,
                    note="typed",
                )
            )
            self.last_screenshot = await self.surface.screenshot()

    async def human_press(self, key: str) -> None:
        async with self.lock:
            if self.control != "human":
                raise RuntimeError("session is not under human control")
            await self.surface.press(key)
            self.human_steps.append(
                Step(id=f"human-{len(self.human_steps)+1}", kind="human", press_key=key, note="press")
            )
            self.last_screenshot = await self.surface.screenshot()

    def snapshot(self) -> dict:
        iv = self.intervention
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "goal": self.goal,
            "control": self.control,
            "step_index": self.step_index,
            "url": self.surface.page.url if self.surface and self.surface.page else "",
            "intervention": None
            if not iv
            else {
                "reason": iv.reason,
                "step_id": iv.step_id,
                "screenshot": iv.screenshot_rel,
            },
            "human_steps": [s.model_dump() for s in self.human_steps],
        }


class SessionStore:
    def __init__(self):
        self._items: dict[str, LiveSession] = {}

    def put(self, session: LiveSession) -> None:
        self._items[session.session_id] = session

    def get(self, session_id: str) -> LiveSession | None:
        return self._items.get(session_id)

    def drop(self, session_id: str) -> None:
        self._items.pop(session_id, None)

    def latest(self) -> LiveSession | None:
        if not self._items:
            return None
        return list(self._items.values())[-1]


STORE = SessionStore()


def _looks_secret(text: str) -> bool:
    return len(text) >= 4 and text.replace(" ", "") == text and not text.isdigit()
