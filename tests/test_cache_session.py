"""Session-durable cache hit counters (T14)."""

from __future__ import annotations


def test_session_cache_accumulates_and_reloads(sessions, workspace):
    handle = sessions.create(workspace)
    handle.add_cache(1000, 400)
    handle.add_cache(500, 500)
    assert handle.meta.cache_prompt_tokens == 1500
    assert handle.meta.cache_cached_tokens == 900
    assert abs(handle.meta.cache_hit_rate - 0.6) < 1e-9

    reloaded = sessions.open(handle.meta.id)
    assert reloaded is not None
    assert reloaded.meta.cache_prompt_tokens == 1500
    assert abs(reloaded.meta.cache_hit_rate - 0.6) < 1e-9


def test_clearing_messages_resets_session_cache(sessions, workspace):
    handle = sessions.create(workspace)
    handle.add_cache(100, 50)
    handle.clear_messages()
    assert handle.meta.cache_prompt_tokens == 0
    assert handle.meta.cache_cached_tokens == 0
