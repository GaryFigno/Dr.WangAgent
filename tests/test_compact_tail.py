"""Compaction recent-tail token budget."""

from __future__ import annotations

from aiharness.agent.context import _split_for_compaction
from aiharness.providers.base import Message


def test_preserve_recent_tokens_grows_tail_beyond_keep_count():
    body = [Message(role="user", content=f"turn-{i} " + ("word " * 80)) for i in range(30)]
    messages = [Message(role="system", content="sys")] + body
    _system, older_count_only, recent_count_only = _split_for_compaction(
        messages, keep_recent=4, preserve_recent_tokens=0
    )
    _system, older, recent = _split_for_compaction(
        messages, keep_recent=4, preserve_recent_tokens=3_000
    )
    assert len(recent_count_only) == 4
    assert len(recent) >= len(recent_count_only)
    assert len(older) + len(recent) == len(body)
