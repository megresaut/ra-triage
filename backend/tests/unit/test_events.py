"""Tests for the in-process pub/sub EventBus."""

from __future__ import annotations

import asyncio

import pytest

from ra_triage.utils.events import EventBus


@pytest.mark.asyncio
async def test_subscribe_then_publish_delivers_payload() -> None:
    bus = EventBus()
    q = await bus.subscribe("bugs:queue")
    await bus.publish("bugs:queue", {"type": "hello", "n": 1})
    payload = await asyncio.wait_for(q.get(), timeout=1.0)
    assert payload == {"type": "hello", "n": 1}


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus()
    # Must not raise.
    await bus.publish("nothing-listens", {"x": 1})


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    q = await bus.subscribe("t")
    await bus.unsubscribe("t", q)
    await bus.publish("t", {"x": 1})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_two_subscribers_both_receive() -> None:
    bus = EventBus()
    q1 = await bus.subscribe("t")
    q2 = await bus.subscribe("t")
    await bus.publish("t", {"k": "v"})
    p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    p2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert p1 == p2 == {"k": "v"}


@pytest.mark.asyncio
async def test_topics_are_isolated() -> None:
    bus = EventBus()
    q_a = await bus.subscribe("topic-a")
    q_b = await bus.subscribe("topic-b")
    await bus.publish("topic-a", {"to": "a"})
    got = await asyncio.wait_for(q_a.get(), timeout=1.0)
    assert got == {"to": "a"}
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q_b.get(), timeout=0.1)


@pytest.mark.asyncio
async def test_publish_sync_delivers_when_loop_running() -> None:
    bus = EventBus()
    q = await bus.subscribe("sync-topic")
    bus.publish_sync("sync-topic", {"via": "sync"})
    payload = await asyncio.wait_for(q.get(), timeout=1.0)
    assert payload == {"via": "sync"}


def test_publish_sync_without_loop_is_silent() -> None:
    bus = EventBus()
    # No running loop in a plain sync test — must not raise.
    bus.publish_sync("anything", {"x": 1})


@pytest.mark.asyncio
async def test_publish_agent_stdout_shape() -> None:
    """``publish_agent_stdout`` emits a standard agent_stdout payload."""
    from ra_triage.utils.events import bus as global_bus
    from ra_triage.utils.events import publish_agent_stdout, topic_bug_logs

    queue = await global_bus.subscribe(topic_bug_logs(77))
    publish_agent_stdout(bug_id=77, role="investigator", attempt=2, text="hello")
    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert payload == {
        "type": "agent_stdout",
        "bug_id": 77,
        "role": "investigator",
        "attempt": 2,
        "text": "hello",
    }
    await global_bus.unsubscribe(topic_bug_logs(77), queue)


def test_get_loop_returns_none_without_attachment() -> None:
    """Fresh bus with no attached loop returns None."""
    bus = EventBus()
    assert bus.get_loop() is None
