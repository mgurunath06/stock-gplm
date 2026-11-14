"""Process-local issuance seals for adapter-produced eligibility (M5.1).

Trust boundary. ``STRICT_RESEARCH`` status may only come from
:class:`stock_gplm.m5.adapter.HistoricalDataAdapter` working over evidence that
:func:`stock_gplm.m5.evidence.load_reference_evidence` read from files. Every
object those two produce carries an HMAC-SHA256 seal over its content hash. The
runner, the run loader and the Studio treat an object as adapter-issued only
when its seal verifies.

Threat model. The seal defends against:

* caller-constructed ``DataEligibilityDecision`` / ``VerifiedHistoricalDataset``
  objects whose content hashes are internally consistent (a matching hash alone
  never establishes provenance);
* evidence objects edited after loading (``dataclasses.replace`` and similar);
* decisions decoded from JSON, pickles or earlier runs, and objects issued in
  another process (the key never leaves this process);
* reuse of a genuine decision for different bars, requests or evidence (the seal
  covers the decision identifier, which binds all of them).

It does not defend against code running inside this interpreter that
deliberately reads the closure key or patches this package. Python has no memory
isolation, and such code could equally rewrite the runner. That attacker is out
of scope, and the docs say so.

Lifetime and storage. One 256-bit key is drawn from :mod:`secrets` at import. It
is held only in a closure, never logged, serialized or written to disk, and dies
with the process. Seals are therefore useless outside the issuing process.

Replay. Re-presenting a sealed object is permitted only for exactly the content
it was issued for. That is idempotent and harmless: the same evidence, request
and bars give the same decision. Any change to that content changes the hash and
voids the seal.

The sealing function is deliberately not exported. Only verification is.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Callable


def _issuer() -> tuple[Callable[[str, str], str], Callable[[str, str, object], bool]]:
    key = secrets.token_bytes(32)

    def seal(kind: str, digest: str) -> str:
        return hmac.new(key, f"{kind}\x00{digest}".encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(kind: str, digest: str, value: object) -> bool:
        return isinstance(value, str) and hmac.compare_digest(seal(kind, digest), value)

    return seal, verify


_seal, verify_seal = _issuer()
