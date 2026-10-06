"""Start Redis and the five Orbit services for one bundled Mac app.

The parent process passes keys in the environment. This file never prints
those values. It writes only the device token and the local service URLs,
which the menu-bar app already expects.
"""
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

PORTS = {"task": 18100, "voice": 18101, "automation": 18102, "research": 18103, "decision": 18104}
REDIS_PORT = 16379
CAPABILITIES = "computer,research,skill,answer,clarify,start_dictation,open_notes,open_chrome,open_spotify"


def missing_names(env):
    missing = []
    for name in ("ORBIT_DEVICE_TOKEN", "ORBIT_SERVICE_TOKEN"):
        if len(env.get(name, "").strip()) < 32:
            missing.append(name)
    device, service = env.get("ORBIT_DEVICE_TOKEN", "").strip(), env.get("ORBIT_SERVICE_TOKEN", "").strip()
    if device and device == service:
        missing.append("distinct service token")
    for name in ("OPENAI_API_KEY",):
        if len(env.get(name, "").strip()) < 8:
            missing.append(name)
    return missing


def skill_roots(home):
    learned = home / "Library/Application Support/Orbit/learned-skills"
    learned.mkdir(parents=True, exist_ok=True)
    candidates = [home / ".codex/skills", home / ".claude/skills", home / ".agents/skills",
                  home / ".claude/plugins", learned]
    return ",".join(str(path) for path in candidates if path.exists())


def service_environment(parent, service, runtime, site, data, home):
    learned = home / "Library/Application Support/Orbit/learned-skills"
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": parent.get("TMPDIR", "/tmp"),
        "ORBIT_SERVICE": service,
        "ORBIT_BIND_HOST": "127.0.0.1",
        "ORBIT_PORT": str(PORTS[service]),
        "ORBIT_DEVICE_TOKEN": parent["ORBIT_DEVICE_TOKEN"].strip(),
        "ORBIT_SERVICE_TOKEN": parent["ORBIT_SERVICE_TOKEN"].strip(),
        "REDIS_URL": f"redis://127.0.0.1:{REDIS_PORT}/0",
        "ORBIT_TASK_URL": f"http://127.0.0.1:{PORTS['task']}",
        "ORBIT_DECISION_URL": f"http://127.0.0.1:{PORTS['decision']}",
        "ORBIT_ARTIFACT_DIR": str(data / "artifacts"),
        "PYTHONPATH": os.pathsep.join([str(site), str(runtime / "shared"), str(runtime / "services" / service)]),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ORBIT_JEV_MODEL": "jev-1.13.0",
        "ORBIT_REALTIME_MODEL": "gpt-realtime-2.1-mini",
        "ORBIT_TRANSCRIBE_MODEL": "gpt-4o-transcribe",
        "ORBIT_AGENT_MODEL": "gpt-5.6-sol",
    }
    if service in {"voice", "automation", "research"}:
        env["OPENAI_API_KEY"] = parent["OPENAI_API_KEY"].strip()
    if service == "decision":
        env["TYPESAFE_API_KEY"] = parent.get("TYPESAFE_API_KEY", "").strip()
    if service == "voice":
        env["ORBIT_JEV_MODE"] = "off"
        env["ORBIT_JEV_CAPABILITIES"] = CAPABILITIES
        env["ORBIT_SKILL_ROOTS"] = skill_roots(home)
    if service == "automation":
        env["ORBIT_SKILL_ROOTS"] = skill_roots(home)
        env["ORBIT_LEARNED_SKILLS_DIR"] = str(learned)
    return env


def device_config(token):
    return {
        "ORBIT_DEVICE_TOKEN": token.strip(),
        "ORBIT_TASK_URL": f"http://127.0.0.1:{PORTS['task']}",
        "ORBIT_VOICE_URL": f"ws://127.0.0.1:{PORTS['voice']}",
    }


def write_device_config(path, token):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(device_config(token), stream, indent=2)
        stream.write("\n")


def redis_ready(port, attempts=50):
    for _ in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2) as conn:
                conn.sendall(b"PING\r\n")
                if b"PONG" in conn.recv(32):
                    return True
        except OSError:
            time.sleep(0.1)
    return False


def services_ready():
    for port in PORTS.values():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                if response.status != 200:
                    return False
        except (OSError, urllib.error.URLError):
            return False
    return True


def port_free(port):
    for _ in range(50):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def stop_previous(pidfile):
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        return
    try:
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True)
    except (subprocess.CalledProcessError, OSError):
        return
    if "host.py" not in command:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return
    for port in (REDIS_PORT, *PORTS.values()):
        port_free(port)


def serve(env, runtime, python, redis_bin, data):
    missing = missing_names(env)
    if missing:
        raise SystemExit("Orbit is missing " + ", ".join(missing))
    home = Path(env.get("HOME") or str(Path.home()))
    support = home / "Library/Application Support/Orbit"
    data.mkdir(parents=True, exist_ok=True)
    (data / "artifacts").mkdir(parents=True, exist_ok=True)
    (data / "redis").mkdir(parents=True, exist_ok=True)
    site = Path(env["ORBIT_SITE_PACKAGES"])
    write_device_config(support / "config.json", env["ORBIT_DEVICE_TOKEN"])
    pidfile = data / "host.pid"
    stop_previous(pidfile)
    try:
        os.setpgrp()
    except OSError:
        pass
    pidfile.write_text(str(os.getpid()) + "\n")
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    children = []

    def shutdown(signum=None, _frame=None):
        for process in children:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in children:
            try:
                process.wait(max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
        raise SystemExit(0 if signum else 1)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    redis_log = open(logs / "redis.log", "ab")
    children.append(subprocess.Popen(
        [str(redis_bin), "--bind", "127.0.0.1", "--port", str(REDIS_PORT), "--dir", str(data / "redis"),
         "--appendonly", "yes", "--maxmemory", "128mb", "--maxmemory-policy", "noeviction",
         "--protected-mode", "yes", "--daemonize", "no"],
        stdout=redis_log, stderr=subprocess.STDOUT))
    if not redis_ready(REDIS_PORT):
        shutdown()
    for service in ("task", "decision", "voice", "automation", "research"):
        log = open(logs / f"{service}.log", "ab")
        child_env = service_environment(env, service, runtime, site, data, home)
        children.append(subprocess.Popen(
            [str(python), str(runtime / "scripts" / "serve.py")],
            env=child_env, cwd=str(data), stdout=log, stderr=subprocess.STDOUT))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in children):
            raise SystemExit("An Orbit service stopped during startup.")
        if services_ready():
            print("ORBIT_READY", flush=True)
            while all(process.poll() is None for process in children):
                time.sleep(1)
            raise SystemExit("An Orbit service stopped.")
        time.sleep(0.2)
    raise SystemExit("Orbit services did not become ready.")


def main():
    runtime = Path(os.environ["ORBIT_RUNTIME"])
    data = Path(os.environ.get("ORBIT_DATA_DIR", Path.home() / "Library/Application Support/Orbit/runtime"))
    serve(os.environ, runtime, Path(os.environ["ORBIT_PYTHON"]), Path(os.environ["ORBIT_REDIS_BIN"]), data)


if __name__ == "__main__":
    main()
