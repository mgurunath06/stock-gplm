"""Process-wide wall-clock deadline for the single-threaded auth service."""
from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from types import FrameType
from typing import Iterator

from .errors import OperationDeadlineExceeded, SecurityBoundaryError

AUTH_OPERATION_TIMEOUT_SECONDS = 360


@contextmanager
def authentication_deadline(seconds: int = AUTH_OPERATION_TIMEOUT_SECONDS) -> Iterator[None]:
    """Bound the whole auth attempt with a real-time POSIX timer.

    The production CLI is intentionally single-threaded. Refusing other threads
    avoids offering a deadline that Python cannot enforce with ``SIGALRM``.
    """
    if threading.current_thread() is not threading.main_thread():
        raise SecurityBoundaryError("authentication must run in the main service thread")

    def expired(_signum: int, _frame: FrameType | None) -> None:
        raise OperationDeadlineExceeded("authentication wall-clock deadline expired")

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
