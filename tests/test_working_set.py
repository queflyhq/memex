"""WorkingSet (L1 cache) behavior tests."""

from __future__ import annotations

from memex.core.working_set import WorkingSet


def test_capacity_enforced():
    ws = WorkingSet(capacity=3)
    for i in range(10):
        ws.touch(f"c_{i}")
    assert len(ws) == 3


def test_contains_after_touch():
    ws = WorkingSet(capacity=10)
    ws.touch("c_a")
    assert ws.contains("c_a")


def test_lru_eviction_order():
    ws = WorkingSet(capacity=3)
    ws.touch("a")
    ws.touch("b")
    ws.touch("c")
    ws.touch("d")  # a should evict
    assert not ws.contains("a")
    assert ws.contains("b") and ws.contains("c") and ws.contains("d")


def test_re_touch_promotes():
    ws = WorkingSet(capacity=3)
    ws.touch("a")
    ws.touch("b")
    ws.touch("c")
    ws.touch("a")  # bumps a to most-recent
    ws.touch("d")  # b should evict, not a
    assert ws.contains("a")
    assert not ws.contains("b")


def test_bias_returns_higher_when_present():
    ws = WorkingSet(capacity=10)
    ws.touch("a")
    assert ws.bias("a") > ws.bias("z")
