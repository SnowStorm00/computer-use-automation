# computer-use-automation

Record a back-office flow with an LLM, save it as a typed capability, then replay it without the model. Built against a local mock of a legacy core-servicing screen (tables, an iframe, no test ids).

## Setup

Python 3.11+. From the repo root:

```
python -m pip install -e .
python -m playwright install chromium
```

Copy `.env.example` to `.env`. Discovery needs `OPENAI_API_KEY`. Replay and the mock bank do not. Sign-on for the mock app is `teller01` / `demo` (also the defaults in `.env`). Do not put real credentials anywhere.

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
CUA_OPERATOR_PASSWORD=demo
```

PowerShell:

```
$env:OPENAI_API_KEY="sk-..."
```

## Demo

Everything below starts the mock bank on `http://127.0.0.1:8080/bank` if it is not already up.

### 1. Discover (LLM in the loop)

```
python -m cua discover --goal "look up member 12345 and read their current savings balance"
```

That writes `artifacts/<slug>.json` and a run folder under `runs/discover/`. To pin the output path:

```
python -m cua discover --goal "look up member 12345 and read their current savings balance" --out evidence/discovered.json
```

`--headed` if you want to watch the browser.

### 2. Replay (no LLM)

Happy path:

```
python -m cua replay --artifact evidence/lookup_member_savings.json --params "{\"member_id\": \"12345\"}"
```

Expected business outcome (unknown member, not a crash):

```
python -m cua replay --artifact evidence/lookup_member_savings.json --params "{\"member_id\": \"99999\"}"
```

Restricted member:

```
python -m cua replay --artifact evidence/lookup_member_savings.json --params "{\"member_id\": \"11111\"}"
```

### 3. Human takeover

```
python -m cua replay --artifact evidence/lookup_member_savings.json --params "{\"member_id\": \"00000\"}"
```

Member `00000` raises an unexpected "core processing hold" dialog the artifact does not know how to dismiss. Replay pauses the **same** Playwright session, prints nothing fancy, and waits. Open:

```
http://127.0.0.1:8080/operator
```

Click the live screenshot, type if you need to, then **Hand back / resume**.

### Operator console only

```
python -m cua serve
```

Bank sign-on: `http://127.0.0.1:8080/bank/login`. Operator page: `http://127.0.0.1:8080/operator`. Calling agents can also `POST /api/capabilities/lookup_member_savings/invoke` with `{"params": {"member_id": "12345"}}`.

## Tests

```
python -m pytest -q
```

Replay tests hit the live mock app through Playwright. They do not call OpenAI.

## Layout

- `src/cua/bank/` — CoreLink mock (iframe workspace, table layout)
- `src/cua/artifact/schema.py` — capability schema
- `src/cua/agent/` — observe → decide → act
- `src/cua/replay/` — deterministic executor + exception rules
- `src/cua/session.py` — control owner for the live session
- `src/cua/operator/` — minimal operator page
- `evidence/` — saved artifact plus discovery/replay logs

See `REPORT.md` for why it is shaped this way.
