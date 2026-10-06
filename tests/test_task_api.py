import fakeredis.aioredis
from fastapi.testclient import TestClient

from orbit_common.config import Settings
from orbit_task.app import create_app


def test_device_credentials_cannot_impersonate_worker(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_app(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))) as client:
        assert client.post("/v1/sessions", json={"device_id": "mac"}).status_code == 401
        device = {"Authorization": "Bearer " + "d" * 40}
        session = client.post("/v1/sessions", json={"device_id": "mac"}, headers=device).json()
        task = client.post("/v1/tasks", headers=device, json={
            "session_id": session["session_id"], "device_id": "mac", "kind": "research", "goal": "solar cells"
        }).json()
        denied = client.post(f"/v1/internal/tasks/{task['task_id']}/claim", headers=device,
                             json={"worker_id": "attacker"})
        assert denied.status_code == 401
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200


def test_device_disconnect_cancels_live_tasks(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_app(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))) as client:
        device = {"Authorization": "Bearer " + "d" * 40}
        sid = client.post("/v1/sessions", json={"device_id": "mac"}, headers=device).json()["session_id"]
        with client.websocket_connect(f"/v1/devices/mac/ws?session_id={sid}", headers=device) as ws:
            assert ws.receive_json()["type"] == "connected"
            assert ws.receive_json()["type"] == "followup.snapshot"
            task = client.post("/v1/tasks", headers=device, json={
                "session_id": sid, "device_id": "mac", "kind": "automation", "goal": "open notes"
            }).json()
            assert ws.receive_json()["task"]["task_id"] == task["task_id"]
        assert client.get(f"/v1/tasks/{task['task_id']}", headers=device).json()["status"] == "cancelled"


def test_worker_can_publish_progress_and_complete_through_http_contract(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token="d" * 40, orbit_service_token="s" * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_app(settings, fakeredis.aioredis.FakeRedis(decode_responses=True)),
                    raise_server_exceptions=False) as client:
        device = {"Authorization": "Bearer " + "d" * 40}
        service = {"Authorization": "Bearer " + "s" * 40}
        sid = client.post("/v1/sessions", json={"device_id": "mac"}, headers=device).json()["session_id"]
        task = client.post("/v1/tasks", headers=device, json={"session_id": sid, "device_id": "mac",
                           "kind": "research", "goal": "Compare sources"}).json()
        path = f"/v1/internal/tasks/{task['task_id']}"
        claim = client.post(path + "/claim", headers=service, json={"worker_id": "test-worker"}).json()
        owner = {"worker_id": "test-worker", "fence": claim["fence"]}
        assert client.post(path + "/heartbeat", headers=service, json=owner).status_code == 200
        progress = client.post(path + "/event", headers=service, json={**owner, "progress": "Formatting",
                                                                                   "result": {"sources": 3}})
        assert progress.status_code == 200
        assert progress.json()["progress"] == "Formatting"
        assert progress.json()["status"] == "running"
        completion = client.post(path + "/event", headers=service, json={**owner, "status": "completed",
                                                               "result": {"summary": "Ready"}})
        assert completion.status_code == 200
        assert completion.json()["status"] == "completed"
        assert client.get(f"/v1/tasks/{task['task_id']}", headers=device).json()["result"] == {"summary": "Ready"}


def test_followup_confirmation_is_device_owned_and_stale_generation_is_rejected(tmp_path):
    settings = Settings(_env_file=None, orbit_device_token='d' * 40, orbit_service_token='s' * 40,
                        orbit_artifact_dir=tmp_path)
    with TestClient(create_app(settings, fakeredis.aioredis.FakeRedis(decode_responses=True))) as client:
        device = {'Authorization': 'Bearer ' + 'd' * 40}
        service = {'Authorization': 'Bearer ' + 's' * 40}
        sid = client.post('/v1/sessions', json={'device_id': 'mac'}, headers=device).json()['session_id']
        path = f'/v1/sessions/{sid}/followups'
        proposal = {'request_id': 'one', 'generation': 0, 'spec': {'text': 'Check my intention',
            'due_at': '2026-10-06T10:00:00+05:30', 'timezone': 'Asia/Kolkata', 'repeat': 'daily'}}
        rule = client.post(path, json=proposal, headers=service).json()
        assert rule['status'] == 'proposed'
        confirm = path + '/' + rule['id'] + '/confirm'
        assert client.post(confirm, json={'version': rule['version']}, headers=service).status_code == 401
        assert client.post(confirm, json={'version': rule['version']}, headers=device).json()['status'] == 'active'
        client.post(f'/v1/sessions/{sid}/stop', headers=device)
        assert client.post(path, json={**proposal, 'request_id': 'two'}, headers=service).status_code == 409
        assert client.get(path, headers=device).json()['rules'][0]['status'] == 'active'
        assert client.delete(path + '/' + rule['id'], headers=service).json()['status'] == 'cancelled'


def test_spoken_dismissal_emits_followup_change_for_native_client(tmp_path):
    from datetime import datetime, timedelta, timezone
    settings = Settings(_env_file=None, orbit_device_token='d' * 40, orbit_service_token='s' * 40,
                        orbit_artifact_dir=tmp_path)
    db = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with TestClient(create_app(settings, db)) as client:
        device = {'Authorization': 'Bearer ' + 'd' * 40}
        service = {'Authorization': 'Bearer ' + 's' * 40}
        sid = client.post('/v1/sessions', json={'device_id': 'mac'}, headers=device).json()['session_id']
        path = f'/v1/sessions/{sid}/followups'
        rule = client.post(path, headers=service, json={'request_id': 'one', 'spec': {
            'text': 'Reminder', 'due_at': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            'timezone': 'UTC', 'repeat': 'none'}}).json()
        client.post(path + '/' + rule['id'] + '/confirm', headers=device, json={'version': 1})
        # Inject delivery independent of wall-clock quiet hours to exercise receipt routing.
        async def prepare():
            from orbit_task.followups import FollowUps
            records = await FollowUps(db).list('mac')
            records[0]['delivery'] = {'delivery_id': 'delivery', 'rule_id': rule['id'], 'version': 1, 'text': 'Reminder'}
            await FollowUps(db)._save('mac', {rule['id']: records[0]})
        client.portal.call(prepare)
        with client.websocket_connect(f'/v1/sessions/{sid}/events', headers=service) as ws:
            ws.receive_json()
            ws.receive_json()
            result = client.post(f'/v1/sessions/{sid}/followup-receipts', headers=service, json={'delivery_id': 'delivery'})
            assert result.status_code == 200
            async def latest():
                return await db.xrevrange('orbit:device:mac', count=1)
            import json
            event = json.loads(client.portal.call(latest)[0][1]['json'])
            assert event['type'] == 'followup.changed'
            assert event['rule']['status'] == 'completed'
