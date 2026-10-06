from orbit_voice import events


def task(version=1, status='running', phase='planning', generation=0):
    return dict(task_id='one', version=version, status=status, phase=phase, generation=generation,
                goal='Arrange desktop', progress='Checking the current window')


def test_progress_is_delayed_coalesced_and_never_replays_snapshots():
    cls = getattr(events, 'Engagement', None)
    assert cls is not None
    policy = cls()
    policy.update(task(), now=0)
    assert policy.next(now=3) is None
    assert policy.next(now=4)['status'] == 'running'
    policy.update(task(2, phase='observing'), now=5)
    policy.update(task(3, phase='acting'), now=6)
    assert policy.next(now=18) is None
    assert policy.next(now=19)['phase'] == 'acting'
    assert policy.next(now=40) is None
    policy.update(task(4, 'completed'), now=41, snapshot=True)
    assert policy.next(now=50) is None


def test_less_commentary_preserves_outcomes_and_generation_reset_discards_pending():
    cls = getattr(events, 'Engagement', None)
    assert cls is not None
    policy = cls()
    policy.routine = False
    policy.update(task(), now=0)
    assert policy.next(now=10) is None
    policy.update(task(2, 'blocked'), now=11)
    assert policy.next(now=11)['status'] == 'blocked'
    policy.update(task(3, 'completed'), now=12)
    policy.reset(1)
    assert policy.next(now=13) is None
    policy.update(task(4, 'completed'), now=14)
    assert policy.next(now=15) is None
