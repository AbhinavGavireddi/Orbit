"""Run independent Python processes; Redis must already be running."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("task", "voice", "automation", "research", "decision", "room")
PROVIDER_KEYS = {"OPENAI_API_KEY", "TYPESAFE_API_KEY"}


def service_env(name, values):
    env = {key: value for key, value in os.environ.items() if key not in PROVIDER_KEYS}
    env.update({key: value for key, value in values.items() if value is not None and key not in PROVIDER_KEYS})
    if name in {"voice", "automation", "research"}:
        env["OPENAI_API_KEY"] = values.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if name == "decision":
        env["TYPESAFE_API_KEY"] = values.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_API_KEY", "")
    env["ORBIT_SERVICE"] = name
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in [ROOT / "shared", *(ROOT / "services" / n for n in NAMES)])
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=(*NAMES, "all"), default="all", nargs="?")
    args = parser.parse_args()
    values = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}
    children = []

    def stop(*_):
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for name in NAMES if args.service == "all" else (args.service,):
        children.append(subprocess.Popen([sys.executable, "scripts/serve.py"], cwd=ROOT,
                                         env=service_env(name, values)))
        if name == "task":
            time.sleep(1)
    while all(child.poll() is None for child in children):
        time.sleep(0.5)
    stop()


if __name__ == "__main__":
    main()
