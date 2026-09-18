from __future__ import annotations

import re
import time

from playwright.async_api import Frame, Locator, Page, async_playwright

from cua.artifact.schema import FrameRef, LocatorCandidate, Target
from cua.safety.policy import Policy, PolicyViolation
from cua.surface.base import Observation


class WebSurface:
    def __init__(self, page: Page, policy: Policy):
        self.page = page
        self.policy = policy
        self._pw = None
        self._browser = None

    @classmethod
    async def launch(cls, policy: Policy, headless: bool = True) -> "WebSurface":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1100, "height": 720})
        page = await context.new_page()
        surface = cls(page, policy)
        surface._pw = pw
        surface._browser = browser
        return surface

    def _check_url(self, url: str) -> None:
        if not self.policy.origin_allowed(url):
            raise PolicyViolation(f"url not on allowlist: {url}")

    async def goto(self, url: str) -> None:
        self._check_url(url)
        await self.page.goto(url, wait_until="domcontentloaded")

    async def current_url(self) -> str:
        return self.page.url

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    def _frame(self, frames: list[FrameRef]) -> Page | Frame:
        ctx: Page | Frame = self.page
        for ref in frames:
            found = None
            if isinstance(ctx, Page):
                children = ctx.frames
            else:
                children = [ctx, *ctx.child_frames]
            for fr in children:
                if ref.by == "name" and (fr.name == ref.value):
                    found = fr
                    break
                if ref.by == "title":
                    try:
                        if fr.name == ref.value:
                            found = fr
                            break
                    except Exception:
                        pass
                if ref.by == "url_substr" and ref.value in (fr.url or ""):
                    found = fr
                    break
            if found is None and isinstance(ctx, Page):
                named = ctx.frame(name=ref.value)
                if named:
                    found = named
            if found is None:
                raise LookupError(f"frame not found: {ref.by}={ref.value}")
            ctx = found
        return ctx

    def _locator(self, ctx: Page | Frame, cand: LocatorCandidate) -> Locator:
        loc: Locator
        if cand.strategy == "role":
            loc = ctx.get_by_role(cand.role or "button", name=cand.value, exact=cand.exact)
        elif cand.strategy == "label":
            loc = ctx.get_by_label(cand.value, exact=cand.exact)
        elif cand.strategy == "placeholder":
            loc = ctx.get_by_placeholder(cand.value, exact=cand.exact)
        elif cand.strategy == "name_attr":
            loc = ctx.locator(f"[name='{cand.value}']")
        elif cand.strategy == "text":
            loc = ctx.get_by_text(cand.value, exact=cand.exact)
        elif cand.strategy == "cell_after_label":
            row = ctx.locator("tr").filter(has_text=re.compile(re.escape(cand.value)))
            loc = row.locator("td").nth(1)
        elif cand.strategy == "css":
            loc = ctx.locator(cand.value)
        else:
            raise LookupError(f"unknown locator strategy {cand.strategy}")
        if cand.nth is not None:
            loc = loc.nth(cand.nth)
        return loc

    async def resolve(self, target: Target, timeout_ms: int) -> Locator:
        ctx = self._frame(target.frames)
        last_err = None
        for cand in target.locators:
            loc = self._locator(ctx, cand)
            try:
                await loc.first.wait_for(state="visible", timeout=timeout_ms)
                return loc.first
            except Exception as exc:
                last_err = exc
                continue
        raise LookupError(f"could not resolve {target.description}: {last_err}")

    async def click(self, target: Target, timeout_ms: int = 15000) -> None:
        if not self.policy.action_allowed("click"):
            raise PolicyViolation("click is not allowed")
        loc = await self.resolve(target, timeout_ms)
        await loc.click(timeout=timeout_ms)
        await self.page.wait_for_timeout(250)

    async def fill(self, target: Target, text: str, timeout_ms: int = 15000) -> None:
        if not self.policy.action_allowed("fill"):
            raise PolicyViolation("fill is not allowed")
        loc = await self.resolve(target, timeout_ms)
        await loc.fill(text, timeout=timeout_ms)

    async def press(self, key: str) -> None:
        if not self.policy.action_allowed("press"):
            raise PolicyViolation("press is not allowed")
        await self.page.keyboard.press(key)

    async def extract(self, target: Target, timeout_ms: int = 15000) -> str:
        if not self.policy.action_allowed("extract"):
            raise PolicyViolation("extract is not allowed")
        loc = await self.resolve(target, timeout_ms)
        text = await loc.inner_text()
        return text.strip()

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png", full_page=False)

    async def page_text(self) -> str:
        chunks = []
        for fr in self.page.frames:
            try:
                chunks.append(await fr.inner_text("body"))
            except Exception:
                continue
        return "\n".join(chunks)

    async def visible_text(self, text: str, timeout_ms: int = 2000) -> bool:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while True:
            blob = await self.page_text()
            if text in blob:
                return True
            if time.monotonic() >= deadline:
                return False
            await self.page.wait_for_timeout(100)

    async def url_matches(self, pattern: str) -> bool:
        compiled = re.compile(pattern)
        if compiled.search(self.page.url):
            return True
        for fr in self.page.frames:
            if compiled.search(fr.url or ""):
                return True
        return False

    async def click_xy(self, x: float, y: float) -> Target | None:
        handle = await self.page.evaluate_handle(
            "([x, y]) => document.elementFromPoint(x, y)",
            [x, y],
        )
        await self.page.mouse.click(x, y)
        await self.page.wait_for_timeout(250)
        try:
            return await describe_handle(self.page, handle)
        except Exception:
            return None

    async def type_text(self, text: str) -> None:
        await self.page.keyboard.type(text, delay=20)

    async def observe(self) -> Observation:
        self._check_url(self.page.url)
        frames = []
        aria_parts = []
        for fr in self.page.frames:
            name = fr.name or ""
            frames.append({"name": name, "url": fr.url, "title": ""})
            try:
                snap = await fr.locator("body").aria_snapshot()
            except Exception:
                try:
                    snap = await fr.inner_text("body")
                except Exception:
                    snap = "(unreadable frame)"
            aria_parts.append(f"[frame name={name or 'top'} url={fr.url}]\n{snap}")
        title = await self.page.title()
        return Observation(
            url=self.page.url,
            title=title,
            frames=frames,
            aria="\n\n".join(aria_parts),
            dialogs=_detect_dialogs(aria_parts),
        )


def _detect_dialogs(aria_parts: list[str]) -> list[str]:
    hits = []
    joined = "\n".join(aria_parts)
    for needle in (
        "Session expired",
        "Core processing hold",
        "Access denied",
        "No member found",
        "Sign-on failed",
    ):
        if needle in joined:
            hits.append(needle)
    return hits


async def describe_locator(page: Page, loc: Locator, frames: list[FrameRef]) -> Target:
    role = None
    acc_name = None
    name_attr = await loc.get_attribute("name")
    placeholder = await loc.get_attribute("placeholder")
    tag = (await loc.evaluate("el => el.tagName")) or ""
    input_type = await loc.get_attribute("type")
    text = ""
    try:
        text = (await loc.inner_text()).strip()
    except Exception:
        pass
    if not text:
        text = (await loc.get_attribute("value")) or ""
    try:
        acc_name = await loc.get_attribute("aria-label")
    except Exception:
        acc_name = None
    if tag == "INPUT" and input_type == "submit":
        role = "button"
        acc_name = acc_name or (await loc.get_attribute("value"))
    elif tag == "INPUT" and input_type == "password":
        role = "textbox"
    elif tag == "INPUT":
        role = "textbox"
    elif tag == "A":
        role = "link"
        acc_name = acc_name or text
    elif tag == "SELECT":
        role = "combobox"
    elif tag == "BUTTON":
        role = "button"
        acc_name = acc_name or text

    locators: list[LocatorCandidate] = []
    if role and (acc_name or text):
        locators.append(
            LocatorCandidate(
                strategy="role",
                role=role,
                value=acc_name or text,
                exact=False,
            )
        )
    if name_attr:
        locators.append(LocatorCandidate(strategy="name_attr", value=name_attr))
    if placeholder:
        locators.append(LocatorCandidate(strategy="placeholder", value=placeholder))
    if text and tag in {"A", "BUTTON", "TD"}:
        locators.append(LocatorCandidate(strategy="text", value=text[:80], exact=False))
    if not locators:
        locators.append(LocatorCandidate(strategy="css", value=await _weak_css(loc)))
    desc = acc_name or name_attr or text or tag
    return Target(description=str(desc), frames=frames, locators=locators)


async def describe_handle(page: Page, handle) -> Target | None:
    el = handle.as_element()
    if not el:
        return None
    name_attr = await el.get_attribute("name")
    text = ""
    try:
        text = ((await el.inner_text()) or "").strip()[:80]
    except Exception:
        text = (await el.get_attribute("value")) or ""
    locators = []
    if name_attr:
        locators.append(LocatorCandidate(strategy="name_attr", value=name_attr))
    if text:
        locators.append(LocatorCandidate(strategy="text", value=text, exact=False))
    frames = _frame_path_for(page)
    return Target(description=text or name_attr or "clicked element", frames=frames, locators=locators)


def _frame_path_for(page: Page) -> list[FrameRef]:
    refs = []
    for fr in page.frames:
        if fr == page.main_frame:
            continue
        if fr.name:
            refs.append(FrameRef(by="name", value=fr.name))
    return refs[-1:] if refs else []


async def _weak_css(loc: Locator) -> str:
    return await loc.evaluate(
        """el => {
            if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
            if (el.tagName === 'INPUT' && el.type) return 'input[type="' + el.type + '"]';
            return el.tagName.toLowerCase();
        }"""
    )
