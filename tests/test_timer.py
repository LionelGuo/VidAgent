import time

from vidagent.utils.timer import Timer


def test_timer_measures_elapsed():
    with Timer("unit-test") as t:
        time.sleep(0.05)
    assert t.elapsed >= 0.04


def test_timed_decorator_sync():
    from vidagent.utils.timer import timed

    @timed("deco-test")
    def work():
        time.sleep(0.03)
        return 42

    assert work() == 42
