# REPORT

## 1. Architecture

Single process. FastAPI serves the mock bank and a tiny operator API; Playwright holds one live browser; the CLI drives either discovery or replay against that browser. I did not split this into workers or a queue. The interesting coupling is the **session**, not the network: discovery, replay, and a human have to take turns on the same page.

Two execution paths share a surface adapter (`WebSurface`) and a policy check on every navigation and action:

- **Discover** — an LLM picks the next action from an accessibility dump plus a screenshot. After each action I record locators from the live element, not from the model's prose. When the model says it is done, those records are compiled into a `CapabilityArtifact`.
- **Replay** — no model. Steps, locators, templates, checkpoints, exception rules.

The mock target is a hostile-on-purpose CoreLink clone: 2005 styling, a named `workspace` iframe, `<table>` forms, `name=` attributes, no test ids. That is closer to the real environment than a clean demo store, and it forced the locator story to be about frames and labeled cells instead of CSS.

Trade-off: one process is enough to prove handoff and is easy to run. It would not survive hundreds of concurrent tenants — that is a packaging problem later (session workers, one browser context per run), not a reason to invent Kafka here.

## 2. Artifact schema

The artifact is the product. It is what another agent calls. It is not a transcript.

- `app` binds the flow to a vendor product and version range, not to a tenant. `entry_url` is an instance default; overlays can replace it.
- `parameters` / `outputs` are the call contract. Values that came from a vault (`operator_password`) are `source: secret` and stored as `{{secrets.operator_password}}`, never as the password.
- `steps` are abstract actions (`click`, `fill`, `extract`) with a `Target`: an ordered list of locator candidates plus a frame path. Replay tries them in order.
- `success_checkpoint` is a positive assertion. Finishing the step list is not success by itself.
- `exception_rules` are first-class. Each rule has a detection checkpoint, a class (`business_outcome` | `recoverable` | `hard_failure`), and an action (`return`, dismiss/retry, `escalate`, `fail`).
- `overlays[]` is how a second institution running the same CoreLink build specializes locators without forking the whole capability.

I version the document (`schema_version` + integer `version`) and keep an `approval_state`. Irreversible steps refuse to run unattended until the capability is approved or the caller passes `--allow-risky`.

Locator order is deliberate: role+name when the control actually has one, then label / `name` attribute, then `cell_after_label` for the table UIs this world is full of, then CSS as a last resort. Coordinates are a human-handoff tool, not a replay strategy.

## 3. Determinism & error handling

Replay substitutes `{{member_id}}` (and locator values, so a results-table link can be the member id) and waits on Playwright actionability. It does not ask the model what to do if the page looks different.

After every step it scans exception rules against the live page:

- **business_outcome** — `99999` → `MEMBER_NOT_FOUND`; `11111` → `ACCESS_DENIED`. The caller gets `status: business_outcome` and a code. That is a successful invocation of a lookup that found nothing, not a failed robot.
- **recoverable** — a known interstitial can be dismissed; an unknown "core processing hold" is not guessed at. It escalates.
- **hard_failure** — session expiry, failed sign-on, or a locator that will not resolve. The result names the step, what was expected, and a redacted slice of what was on screen, plus a screenshot.

UI drift is secondary here on purpose. These apps do not restyle weekly. When they do, the overlay or a re-record is the fix, not an LLM in production.

## 4. Heterogeneity & multi-tenant

The seam is the surface adapter, not the artifact. Steps say "click this target in this frame path." `WebSurface` turns that into Playwright. A desktop adapter would turn the same `role` / `name` locators into UI Automation / AX API calls. CSS locators would not survive that jump, which is why they are last and why I observed via the accessibility tree rather than a pretty DOM dump.

For tenants: record against the vendor product (`corelink` / `member-servicing` / `4.x`). The capability is the shared object. A tenant overlay carries:

- a different entry URL / SSO start
- locator rewrites for relabeled buttons
- extra exception rules for that CU's interstitial ("accept acceptable use")

Detecting drift is a replay problem: if an overlay-less run fails a locator that used to pass, mark the capability stale for that tenant/version and either apply an overlay or send it back through discovery. I did not build a fleet of version crawlers. The schema has a place to put the answer.

## 5. Escalation & handoff

Stuck means: the model asks for a human, a locator throws, a risky step is gated, or an exception rule says `escalate`. The run sets `control = human` on the **existing** `LiveSession`, keeps the Playwright page open, and waits on an asyncio event.

The operator page is intentionally dumb. It polls the session, shows the current screenshot, and forwards clicks (viewport coordinates on that same page) and keystrokes. Resume flips `control` back to `agent` and the executor continues from the next step on whatever state the human left. Human actions are appended to the run log. Same session, explicit owner, no second browser.

What I mocked: a real co-browse product (WebRTC, cursor sharing, audit playback). What is real: pause, exclusive control, act on the live page, resume, record.

## 6. Safety

A YAML allowlist: origins, path prefix `/bank`, action kinds. Navigate off-origin raises `PolicyViolation` before Playwright moves. `evaluate_js`, downloads, and uploads are blocked by construction.

Risky vs reversible: opening a sub-account / `Confirm Open` is irreversible. Lookup is not. Discovery will refuse the risky click unless `--allow-risky`. Replay of an irreversible step pauses for a human unless the artifact is `approved`.

Logs and artifacts run through a redactor (SSNs, long account numbers, bearer tokens, keys named password/token/ssn). The teller password never lands in the JSON. Limits: the model still *types* the password into the live field (it has to), and a screenshot of the member record can still show a TIN. I redact the TIN from extracts named `tin`/`ssn`, but pixels are pixels. A production build would mask those regions or skip screenshot persistence for pages tagged as PII.

## 7. Cuts

Left out on purpose: a React operator console, a desktop surface implementation, queues, per-tenant routing, confidence scoring, and LLM fallback during replay. Each of those is a sequel, not a substitute for the vertical slice.

If I had another week I would (1) score locators across N replays and drop the ones that flap, (2) canonicalize paths (`/member/12345` → `/member/{{member_id}}`) as a first-class compiler pass, (3) add a real approval record so `draft` capabilities cannot be invoked by the agent catalog.
