"""Idempotently prepare local configuration without printing credentials."""
import os
from pathlib import Path
import json
import secrets

ROOT = Path(__file__).resolve().parents[1]


def configure(root=ROOT):
    target = root / ".env"
    if not target.exists():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write((root / ".env.example").read_text())
    lines = target.read_text().splitlines()
    for name in ("ORBIT_DEVICE_TOKEN", "ORBIT_SERVICE_TOKEN"):
        for i, line in enumerate(lines):
            if line.startswith(name + "=") and not line.split("=", 1)[1].strip():
                lines[i] = name + "=" + secrets.token_urlsafe(40)
                break
        else:
            if not any(line.startswith(name + "=") for line in lines):
                lines.append(name + "=" + secrets.token_urlsafe(40))
    temporary = root / ".env.tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        stream.write("\n".join(lines) + "\n")
    temporary.replace(target)
    target.chmod(0o600)
    (root / "var/artifacts").mkdir(parents=True, exist_ok=True)

    support = Path.home() / "Library/Application Support/Orbit"
    support.mkdir(parents=True, exist_ok=True)
    values = {}
    for line in target.read_text().splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip()
    config = {
        "ORBIT_DEVICE_TOKEN": values.get("ORBIT_DEVICE_TOKEN", ""),
        "ORBIT_TASK_URL": values.get("ORBIT_TASK_URL") or "http://127.0.0.1:8100",
        "ORBIT_VOICE_URL": values.get("ORBIT_VOICE_URL") or "ws://127.0.0.1:8101",
    }
    if not config["ORBIT_DEVICE_TOKEN"]:
        raise SystemExit("ORBIT_DEVICE_TOKEN missing after setup")
    config_path = support / "config.json"
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    print("Wrote device config for plug-and-play launches (no provider keys).")

    print("Local tokens configured. Add provider keys to .env privately; existing keys were preserved.")


if __name__ == "__main__":
    configure()
