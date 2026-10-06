"""Run one service with the same entrypoint in Docker or a local venv."""
import os

import uvicorn

SERVICES = {"task": 8100, "voice": 8101, "automation": 8102, "research": 8103, "decision": 8104, "room": 8105}

if __name__ == "__main__":
    service = os.environ["ORBIT_SERVICE"]
    if service not in SERVICES:
        raise SystemExit("Unknown Orbit service")
    uvicorn.run(f"orbit_{service}.{'handler' if service in {'automation', 'research'} else 'app'}:create_app",
                factory=True, host=os.getenv("ORBIT_BIND_HOST", "127.0.0.1"),
                port=int(os.getenv("ORBIT_PORT", SERVICES[service])),
                timeout_graceful_shutdown=10, ws_max_size=4_000_000, access_log=False)
