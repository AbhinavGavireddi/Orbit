"""Durable conversational follow-ups. Scheduling never grants execution authority."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from typing import Literal


class FollowUpSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=500)
    due_at: AwareDatetime
    timezone: str = Field(max_length=100)
    repeat: Literal['none', 'daily', 'weekly'] = 'none'

    @field_validator('timezone')
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone") from error
        return value


class FollowUps:
    def __init__(self, redis):
        self.redis = redis

    def key(self, device):
        return 'orbit:followups:' + hashlib.sha256(device.encode()).hexdigest()

    async def list(self, device):
        return list(json.loads(await self.redis.get(self.key(device)) or '{}').values())

    async def _save(self, device, rules):
        await self.redis.set(self.key(device), json.dumps(rules))
        await self.redis.sadd('orbit:followup-devices', device)

    async def propose(self, device, request_id, spec, rule_id=None):
        spec = FollowUpSpec.model_validate(spec).model_dump(mode='json')
        async with self.redis.lock(self.key(device) + ':lock', timeout=5):
            rules = {r['id']: r for r in await self.list(device)}
            request_key = self.key(device) + ':requests'
            prior = await self.redis.hget(request_key, request_id)
            if prior:
                entry = json.loads(prior)
                if entry['spec'] != spec or entry['target'] != rule_id:
                    raise ValueError('Follow-up request ID has different content')
                if entry['id'] not in rules:
                    raise ValueError('This request was retired; use a new request ID')
                return rules[entry['id']]
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, device + ':' + request_id))
            for previous in rules.values():
                if previous.get('request_id') == request_id:
                    if previous['spec'] != spec:
                        raise ValueError('Follow-up request ID has different content')
                    return previous
            if rule_id is not None and rule_id not in rules:
                raise ValueError('Follow-up not found for this device')
            rid = rule_id or identity
            previous = rules.get(rid, {})
            if rid not in rules and len(rules) >= 200:
                retired = next((key for key, rule in rules.items() if rule['status'] in {'completed', 'cancelled'}), None)
                if retired:
                    del rules[retired]
                else:
                    raise ValueError('Follow-up limit reached; cancel an old follow-up first')
            record = {'id': rid, 'request_id': request_id, 'version': previous.get('version', 0) + 1,
                      'spec': spec, 'status': 'proposed', 'next_due': spec['due_at'], 'delivery': None}
            rules[rid] = record
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.set(self.key(device), json.dumps(rules))
                pipe.sadd('orbit:followup-devices', device)
                pipe.hset(request_key, request_id, json.dumps({'spec': spec, 'target': rule_id, 'id': rid}))
                await pipe.execute()
            return record

    async def _change(self, device, rid, transform):
        async with self.redis.lock(self.key(device) + ':lock', timeout=5):
            rules = {r['id']: r for r in await self.list(device)}
            if rid not in rules:
                raise ValueError('Follow-up not found for this device')
            transform(rules[rid])
            await self._save(device, rules)
            return rules[rid]

    async def confirm(self, device, rid, version):
        def apply(rule):
            if rule['version'] != version or rule['status'] not in {'proposed', 'active'}:
                raise ValueError('Follow-up changed; review it again')
            rule['status'] = 'active'
        return await self._change(device, rid, apply)

    async def cancel(self, device, rid):
        def apply(rule):
            if rule['status'] != 'cancelled':
                rule['version'] += 1
            rule['status'], rule['delivery'] = 'cancelled', None
        return await self._change(device, rid, apply)

    async def due(self, device, now=None):
        now = now or datetime.now(timezone.utc)
        async with self.redis.lock(self.key(device) + ':lock', timeout=5):
            rules = {r['id']: r for r in await self.list(device)}
            result = []
            for rule in rules.values():
                local = now.astimezone(ZoneInfo(rule['spec']['timezone']))
                if rule['status'] != 'active' or local.hour < 8 or local.hour >= 22:
                    continue
                if datetime.fromisoformat(rule['next_due']) > now:
                    continue
                if not rule.get('delivery'):
                    delivery_id = f"{rule['id']}:{rule['version']}:{rule['next_due']}"
                    rule['delivery'] = {'delivery_id': delivery_id, 'rule_id': rule['id'],
                                        'text': rule['spec']['text'], 'version': rule['version']}
                result.append(rule['delivery'])
            await self._save(device, rules)
            return result

    async def acknowledge(self, device, delivery_id, now=None):
        now = now or datetime.now(timezone.utc)
        async with self.redis.lock(self.key(device) + ':lock', timeout=5):
            rules = {r['id']: r for r in await self.list(device)}
            changed = []
            for rule in rules.values():
                if (rule.get('delivery') or {}).get('delivery_id') != delivery_id:
                    continue
                repeat = rule['spec']['repeat']
                if repeat == 'none':
                    rule['status'] = 'completed'
                else:
                    zone = ZoneInfo(rule['spec']['timezone'])
                    due = datetime.fromisoformat(rule['next_due']).astimezone(zone)
                    interval = timedelta(days=1 if repeat == 'daily' else 7)
                    elapsed_days = max(0, (now.astimezone(zone).date() - due.date()).days)
                    due += interval * max(1, elapsed_days // interval.days)
                    if due <= now:
                        due += interval
                    rule['next_due'] = due.isoformat()
                rule['delivery'] = None
                rule['version'] += 1
                changed.append(rule)
            await self._save(device, rules)
            return {'acknowledged': True, 'rules': changed}


    async def publish(self, device, session, candidate):
        async with self.redis.lock(self.key(device) + ':lock', timeout=5):
            current = next((r for r in await self.list(device) if r['id'] == candidate['rule_id']), None)
            if not current or current['status'] != 'active' or current.get('delivery') != candidate:
                return False
            key = self.key(device) + ':published'
            if await self.redis.hget(key, current['id']) == candidate['delivery_id']:
                return False
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.xadd('orbit:device:' + device, {'json': json.dumps({
                    'type': 'followup.due', 'session_id': session, 'delivery': candidate})}, maxlen=1000)
                pipe.hset(key, current['id'], candidate['delivery_id'])
                await pipe.execute()
            return True


class FollowUpProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=160)
    generation: int = Field(default=0, ge=0)
    spec: FollowUpSpec
    rule_id: str | None = Field(default=None, max_length=100)


class FollowUpConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)


class FollowUpReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    delivery_id: str = Field(min_length=1, max_length=250)
