"""Evaluate the fixed English corpus using runtime credentials, without app actions.

No secrets or provider response bodies are printed. This writes evidence, never
enables Jev. A timeout is an observed fallback, not a correct classification.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT / "services/decision")]
from orbit_common.config import Settings  # noqa: E402
from orbit_common.evaluation import promotion_report, routing_summary  # noqa: E402
from orbit_decision.app import evaluate, warm_connection  # noqa: E402


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


async def run(args):
    config = Settings(_env_file=ROOT / ".env")
    fixtures = read_rows(ROOT / "evals/routing_english.jsonl")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    async with httpx.AsyncClient(timeout=.4, limits=httpx.Limits(keepalive_expiry=60)) as client:
        warmup = await warm_connection(config, client)
        for fixture in fixtures:
            result = await evaluate({"utterance": fixture["utterance"]}, config, client)
            rows.append({**fixture, **result, "measured_at": datetime.now(timezone.utc).isoformat()})
            # Save after every observation so an interrupted run cannot look complete.
            args.output.write_text("".join(json.dumps(row) + "\n" for row in rows))
            if result.get("error") in {"Jev provider unavailable", "Jev HTTP 401", "Jev HTTP 403", "Jev HTTP 404", "Jev HTTP 422"}:
                break
    report = promotion_report(rows, read_rows(args.baseline) if args.baseline else [],
                              read_rows(args.candidate) if args.candidate else [], config.orbit_jev_model,
                              routing_manifest=fixtures,
                              voice_manifest=json.loads(args.audio_manifest.read_text()) if args.audio_manifest.exists() else None)
    report["connection_warmup"] = warmup
    args.output.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"observations": len(rows), "routing": routing_summary(rows),
                      "errors": dict(Counter(row.get("error") for row in rows if row.get("error"))),
                      "recommended_mode": report["recommended_mode"], "report": str(args.output.with_suffix('.report.json'))}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "var/validation/english-routing.jsonl")
    parser.add_argument("--baseline", type=Path, help="Verified native baseline JSONL, optional")
    parser.add_argument("--candidate", type=Path, help="Verified native Jev comparison JSONL, optional")
    parser.add_argument("--audio-manifest", type=Path, default=ROOT / "var/validation/audio/manifest.json")
    asyncio.run(run(parser.parse_args()))
