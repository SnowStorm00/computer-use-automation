from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from cua.config import settings
from cua.runtime import load_artifact, run_discover, run_replay_artifact, serve_forever


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the bank app plus operator API")
    serve.add_argument("--host", default=settings.cua_host)
    serve.add_argument("--port", type=int, default=settings.cua_port)

    disc = sub.add_parser("discover", help="LLM-driven run; writes a capability artifact")
    disc.add_argument("--goal", required=True)
    disc.add_argument("--url", default=None)
    disc.add_argument("--headed", action="store_true")
    disc.add_argument("--allow-risky", action="store_true")
    disc.add_argument("--out", default=None)

    rep = sub.add_parser("replay", help="deterministic replay of a saved artifact")
    rep.add_argument("--artifact", required=True)
    rep.add_argument("--params", default="{}")
    rep.add_argument("--headed", action="store_true")
    rep.add_argument("--allow-risky", action="store_true")
    rep.add_argument("--tenant", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        settings.cua_host = args.host
        settings.cua_port = args.port
        print(f"bank:      {settings.bank_url}/login")
        print(f"operator:  {settings.base_url}/operator")
        serve_forever()
        return
    if args.cmd == "discover":
        result = asyncio.run(
            run_discover(
                args.goal,
                entry_url=args.url,
                allow_risky=args.allow_risky,
                headless=not args.headed,
            )
        )
        if result.artifact and args.out:
            Path(args.out).write_text(result.artifact.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        print(json.dumps(json.loads(result.model_dump_json()), indent=2))
        if result.status != "success":
            sys.exit(1)
        print(f"operator (if escalated): {settings.base_url}/operator?session={result.session_id}")
        return
    if args.cmd == "replay":
        params = json.loads(args.params)
        artifact = load_artifact(args.artifact)
        result = asyncio.run(
            run_replay_artifact(
                artifact,
                params,
                allow_risky=args.allow_risky,
                tenant_id=args.tenant,
                headless=not args.headed,
            )
        )
        print(json.dumps(json.loads(result.model_dump_json()), indent=2))
        if result.status == "failed":
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
