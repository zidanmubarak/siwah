from __future__ import annotations

import pytest

from siwah.server.events import EventBus, sse_frame


@pytest.mark.asyncio
async def test_event_bus_replay_and_frame():
    bus = EventBus("ses_test")
    first = await bus.publish("status", {"state": "running"})
    second = await bus.publish("message.delta", {"text": "hi"})
    assert first.seq == 1
    assert second.seq == 2
    assert bus.history(1) == [second]
    frame = sse_frame(second)
    assert "event: message.delta" in frame
    assert '"text":"hi"' in frame

    subscription = bus.subscribe(after=1)
    replayed = await anext(subscription)
    assert replayed.id == second.id
    await subscription.aclose()


@pytest.mark.asyncio
async def test_replay_gap_rejects_stale_or_future_cursors():
    bus = EventBus("ses_test", retention=2)
    assert bus.replay_gap(0) is False
    for index in range(4):
        await bus.publish("status", {"index": index})
    # 0 is an explicit full-replay request; 1..2 have been evicted.
    assert bus.replay_gap(0) is False
    assert bus.replay_gap(1) is True
    assert bus.replay_gap(2) is False
    assert bus.replay_gap(3) is False
    assert bus.replay_gap(4) is False
    assert bus.replay_gap(5) is True
