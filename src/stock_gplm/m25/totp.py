"""Small RFC 6238 implementation used without exposing the seed to subprocesses."""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def normalize_seed(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("TOTP seed must be text")
    compact = value.replace(" ", "").upper()
    if not compact or len(compact) > 256 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in compact):
        raise ValueError("TOTP seed is not bounded base32")
    padded = compact + "=" * ((8 - len(compact) % 8) % 8)
    try:
        decoded = base64.b32decode(padded, casefold=False)
    except Exception as exc:
        raise ValueError("TOTP seed is invalid base32") from exc
    if len(decoded) < 10:
        raise ValueError("TOTP seed is too short")
    return decoded


def generate_totp(
    seed: str, *, at_time: float | None = None, period: int = 30, digits: int = 6,
) -> str:
    if period <= 0 or digits not in {6, 8}:
        raise ValueError("unsupported TOTP parameters")
    moment = time.time() if at_time is None else at_time
    counter = int(moment) // period
    digest = hmac.new(normalize_seed(seed), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def seconds_remaining(*, at_time: float | None = None, period: int = 30) -> float:
    moment = time.time() if at_time is None else at_time
    remainder = moment % period
    return float(period if remainder == 0 else period - remainder)
