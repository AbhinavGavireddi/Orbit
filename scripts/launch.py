"""Launch the local app with only device configuration; provider keys stay backend-only."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path.home() / "Library/Application Support/Orbit"
CONFIG = SUPPORT / "config.json"


def sync_device_config(values: dict) -> dict:
    """Write device-only config so Finder/Dock launches work without env vars."""
    SUPPORT.mkdir(parents=True, exist_ok=True)
    config = {
        "ORBIT_DEVICE_TOKEN": (values.get("ORBIT_DEVICE_TOKEN") or "").strip(),
        "ORBIT_TASK_URL": (values.get("ORBIT_TASK_URL") or "http://127.0.0.1:8100").strip(),
        "ORBIT_VOICE_URL": (values.get("ORBIT_VOICE_URL") or "ws://127.0.0.1:8101").strip(),
    }
    if not config["ORBIT_DEVICE_TOKEN"]:
        raise SystemExit("Run python3 scripts/setup.py first (missing ORBIT_DEVICE_TOKEN).")
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", nargs="?", const="listening",
                        choices=["listening", "speaking", "working", "error", "approval"],
                        help="Glass preview only: no microphone, API or permissions")
    args = parser.parse_args()
    app = ROOT / "dist/Orbit.app"
    binary = app / "Contents/MacOS/Orbit"
    if not binary.exists():
        raise SystemExit("Build first: python3 scripts/build_mac.py")
    values = {} if args.preview else dotenv_values(ROOT / ".env")
    env = {k: v for k, v in os.environ.items()
           if k not in {"OPENAI_API_KEY", "TYPESAFE_API_KEY", "ORBIT_SERVICE_TOKEN"}}
    if not args.preview:
        config = sync_device_config(values)
        for key, value in config.items():
            env[key] = value
    # Must open the .app bundle — launching the Mach-O directly skips Info.plist
    # usage strings and TCC aborts on Speech/Microphone prompts.
    cmd = ["open", str(app)]
    if args.preview:
        cmd.extend(["--args", "--preview=" + args.preview])
    # env for `open` is not always inherited by GUI apps; config.json covers device auth.
    # Still pass env for same-session launches where macOS forwards it.
    subprocess.run(cmd, env=env, check=True)
    print("Opened Orbit" + (" preview (no microphone or network)" if args.preview else "")
          + " via app bundle")


if __name__ == "__main__":
    main()
