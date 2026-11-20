import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "adaptive_workers", Path(__file__).parents[1] / "deploy/oracle/live/adaptive_workers.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_workers_ramp_up_and_down_without_restart():
    workers = module.AdaptiveWorkers(minimum=1, maximum=4, start=2, ramp_successes=3, ramp_failures=2)
    for _ in range(3):
        workers.success()
    assert workers.snapshot()["target"] == 3
    for _ in range(3):
        workers.success()
    assert workers.snapshot()["target"] == 4
    workers.failure()
    workers.failure()
    assert workers.snapshot()["target"] == 3
    assert workers.snapshot()["extra_delay"] >= 1


def test_workers_respect_bounds():
    workers = module.AdaptiveWorkers(minimum=1, maximum=2, start=1, ramp_successes=1, ramp_failures=1)
    for _ in range(5):
        workers.success()
    assert workers.snapshot()["target"] == 2
    for _ in range(5):
        workers.failure()
    assert workers.snapshot()["target"] == 1
