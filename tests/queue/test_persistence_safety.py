"""Queue journal failures and unconfirmed external outcomes must stop work."""
import asyncio

import pytest

from xuse.queue import QueueConfig, QueueRunner, QueueStore
from xuse.queue.store import QueueJournalError


def add(store):
    return store.add(account='a', action='post', payload={'text':'test'}, dedup_key='once')


def fail(*args, **kwargs):
    raise OSError('synthetic journal unavailable')


def test_create_failure_does_not_create_memory_only_item(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / 'queue.jsonl')
    monkeypatch.setattr(store, '_append', fail)
    with pytest.raises(OSError):
        add(store)
    assert store.list() == []


@pytest.mark.parametrize('transition', ['status', 'attempt'])
def test_transition_failure_preserves_memory_and_disk(tmp_path, monkeypatch, transition):
    path = tmp_path / 'queue.jsonl'
    store = QueueStore(path)
    item = add(store)
    before = item.model_dump()
    monkeypatch.setattr(store, '_append', fail)
    with pytest.raises(OSError):
        if transition == 'status':
            store.set_status(item.queue_id, 'processing')
        else:
            store.record_attempt(item.queue_id, 'test')
    assert item.model_dump() == before
    assert QueueStore(path).get(item.queue_id).model_dump() == before


@pytest.mark.asyncio
async def test_unpersisted_processing_never_reaches_executor(tmp_path, monkeypatch):
    store = QueueStore(tmp_path / 'queue.jsonl')
    add(store)
    calls = []
    async def execute(item):
        calls.append(item.queue_id)
        return {'success':True}
    runner = QueueRunner(store, executor=execute)
    monkeypatch.setattr(store, '_append', fail)
    with pytest.raises(OSError):
        await runner.drain('a')
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,action_id', [(None,None), ('tool_timeout',None), ('cooldown','reserved')])
async def test_unknown_outcome_is_not_automatically_retried(tmp_path, reason, action_id):
    path = tmp_path / 'queue.jsonl'
    store = QueueStore(path)
    item = add(store)
    calls = []
    async def execute(item):
        calls.append(item.queue_id)
        error = RuntimeError('confirmation lost')
        error.reason, error.action_id = reason, action_id
        raise error
    runner = QueueRunner(store, QueueConfig(min_delay_seconds=0, max_delay_seconds=0), executor=execute)
    assert (await runner.drain('a')).failed == 1
    assert (await runner.drain('a')).executed == []
    restored = QueueRunner(QueueStore(path), executor=execute)
    assert (await restored.drain('a')).executed == []
    assert calls == [item.queue_id]


@pytest.mark.asyncio
async def test_confirmation_persistence_failure_does_not_replay_after_restart(tmp_path, monkeypatch):
    path = tmp_path / 'queue.jsonl'
    store = QueueStore(path)
    item = add(store)
    calls = []
    async def execute(item):
        calls.append(item.queue_id)
        monkeypatch.setattr(store, '_append', fail)
        return {'success':True, 'action_id':'confirmed-action'}
    runner = QueueRunner(store, executor=execute)
    with pytest.raises(QueueJournalError) as error:
        await runner.drain('a')
    assert error.value.action_id == 'confirmed-action'
    assert error.value.queue_id == item.queue_id
    assert store.get(item.queue_id).status == 'processing'
    restored = QueueRunner(QueueStore(path), executor=execute)
    assert (await restored.drain('a')).executed == []
    assert calls == [item.queue_id]
