from datetime import datetime, timezone
import fakeredis.aioredis
import pytest
from orbit_task import followups as module


async def test_followup_requires_consent_survives_sessions_and_cancels(tmp_path):
    cls = getattr(module, 'FollowUps', None)
    assert cls is not None
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scheduler = cls(redis)
    try:
        spec = dict(text='Check the intention we discussed', due_at='2026-10-05T10:00:00+05:30',
                    timezone='Asia/Kolkata', repeat='none')
        rule = await scheduler.propose('mac', 'request', spec)
        assert rule['status'] == 'proposed'
        assert (await scheduler.propose('mac', 'request', spec))['id'] == rule['id']
        now = datetime(2026, 10, 5, 5, tzinfo=timezone.utc)
        assert await scheduler.due('mac', now) == []
        await scheduler.confirm('mac', rule['id'], rule['version'])
        first = await scheduler.due('mac', now)
        assert len(first) == 1
        assert first[0]['text'] == spec['text']
        assert await scheduler.due('mac', now) == first  # Stable delivery identity until acknowledged.
        await scheduler.acknowledge('mac', first[0]['delivery_id'])
        assert await scheduler.due('mac', now) == []
        cancelled = await scheduler.cancel('mac', rule['id'])
        assert cancelled['status'] == 'cancelled'
    finally:
        await redis.aclose()


async def test_followups_defer_quiet_hours_and_stale_approval_cannot_activate_edit():
    cls = getattr(module, 'FollowUps', None)
    assert cls is not None
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scheduler = cls(redis)
    try:
        rule = await scheduler.propose('mac', 'a', dict(text='Daily check-in',
            due_at='2026-10-05T23:00:00+05:30', timezone='Asia/Kolkata', repeat='daily'))
        edited = await scheduler.propose('mac', 'b', dict(text='Updated check-in',
            due_at='2026-10-05T23:00:00+05:30', timezone='Asia/Kolkata', repeat='daily'), rule_id=rule['id'])
        with pytest.raises(ValueError):
            await scheduler.confirm('mac', rule['id'], rule['version'])
        await scheduler.confirm('mac', edited['id'], edited['version'])
        assert await scheduler.due('mac', datetime(2026, 10, 5, 18, tzinfo=timezone.utc)) == []
        assert len(await scheduler.due('mac', datetime(2026, 10, 6, 3, tzinfo=timezone.utc))) == 1
        assert await scheduler.list('other') == []
    finally:
        await redis.aclose()


async def test_old_request_retry_does_not_overwrite_confirmed_edit():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scheduler = module.FollowUps(redis)
    original = dict(text='Original', due_at='2026-10-05T10:00:00+00:00', timezone='UTC', repeat='none')
    try:
        first = await scheduler.propose('mac', 'a', original)
        edited = await scheduler.propose('mac', 'b', {**original, 'text': 'Edited'}, rule_id=first['id'])
        await scheduler.confirm('mac', edited['id'], edited['version'])
        await scheduler.propose('mac', 'a', original)
        rule = (await scheduler.list('mac'))[0]
        assert rule['spec']['text'] == 'Edited'
        assert rule['status'] == 'active'
    finally:
        await redis.aclose()


async def test_cancelled_delivery_cannot_be_published_from_stale_candidate():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scheduler = module.FollowUps(redis)
    try:
        rule = await scheduler.propose('mac', 'one', dict(text='Reminder', due_at='2026-10-05T10:00:00+00:00', timezone='UTC'))
        await scheduler.confirm('mac', rule['id'], rule['version'])
        delivery = (await scheduler.due('mac', datetime(2026, 10, 5, 11, tzinfo=timezone.utc)))[0]
        await scheduler.cancel('mac', rule['id'])
        assert hasattr(scheduler, 'publish'), 'Publication must revalidate under the rule lock'
        assert not await scheduler.publish('mac', 'session', delivery)
        assert await redis.xlen('orbit:device:mac') == 0
    finally:
        await redis.aclose()
