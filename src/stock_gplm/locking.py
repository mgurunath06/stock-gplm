"""Kernel advisory locks shared by Windows and POSIX execution paths.

The lock file must remain in place: unlinking it permits two independent owners.
Windows locks one byte beyond normal lock metadata, allowing read-only probes and
owner diagnostics without mandatory byte-range locks blocking metadata reads.
"""
from __future__ import annotations

import os

LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.LockFileEx.restype = wintypes.BOOL
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.restype = wintypes.BOOL

    def flock(fd: int, operation: int) -> None:
        """Acquire/release an exclusive OS lock; contention raises BlockingIOError."""
        overlap = _Overlapped(Offset=0x7fffffff)
        handle = msvcrt.get_osfhandle(fd)
        if operation == LOCK_UN:
            ok = _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap))
        elif operation in (LOCK_EX, LOCK_EX | LOCK_NB):
            ok = _kernel.LockFileEx(handle, 2 | (1 if operation & LOCK_NB else 0),
                                    0, 1, 0, ctypes.byref(overlap))
        else:
            raise ValueError("only exclusive locks are supported")
        if not ok:
            error = ctypes.get_last_error()
            if error == 33:
                raise BlockingIOError(11, "file lock held by another owner")
            raise ctypes.WinError(error)
else:
    from fcntl import flock
