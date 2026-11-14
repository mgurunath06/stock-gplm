"""Versioned evidence registration and an OS-governed approval trust root (M5.1.2).

Finding C (M5.1.1): a caller that supplies both a trust-policy file *and* its
"out-of-band" SHA-256 controls both sides of the comparison, so it can approve
its own registrations. M5.1.2 removes every caller-facing policy/checksum input.

Trust anchor. The only approval authority is a separately governed trust root,
``/etc/stock-gplm/m5-approval-trust-root.json`` (contract
``stock-gplm.m512-approval-trust-root.v1``). It is selected by a fixed path,
never by a research argument, and is accepted only when:

* the trust root, the policy file it names and the directory holding them are
  regular, non-symlink entries opened with ``O_NOFOLLOW`` and owned by the
  administrative account (uid 0), with no group/other write permission;
* every ancestor directory up to ``/`` is administrator-owned and not
  group/other-writable, so the research account cannot swap a parent;
* the research process is *not* running as the administrative account (a
  process that can rewrite its own trust root has no independent authority);
* the trust root's content hash matches its ``root_id``, its status is
  ``ACTIVE``, it pins the policy's exact file SHA-256, and the policy SHA-256 is
  not in its ``revoked_policy_sha256`` list;
* every reviewer and approval in the policy is authorized by the trust root.

The loaded :class:`ApprovalTrustPolicy` carries a process-local issuance seal. A
policy object constructed by a caller never approves anything. No private key
or secret exists or is stored in this repository; the authority is the OS
account boundary, which the research user cannot cross. See
``docs/m512-critical-remediation.md`` for initialization, rotation and
compromise recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .issuance import _issues, verify_seal
from .models import canonical_hash

REGISTRATION_CONTRACT = "stock-gplm.m511-evidence-registration.v1"
TRUST_POLICY_CONTRACT = "stock-gplm.m511-approval-trust-policy.v1"
TRUST_ROOT_CONTRACT = "stock-gplm.m512-approval-trust-root.v1"
TRUST_ROOT_FILE = "m5-approval-trust-root.json"


@dataclass(frozen=True)
class TrustAnchor:
    """Where the administrative trust root lives and which account must own it.

    The production value is fixed below. It is not a parameter of any research
    API; tests substitute it only by patching this module, which is the
    same-interpreter attacker already declared out of scope.
    """
    directory: Path
    owner_uid: int
    boundary: Path = Path("/")  # every directory from ``directory`` up to this one is checked

    @property
    def root_path(self) -> Path:
        return self.directory / TRUST_ROOT_FILE


_ANCHOR = TrustAnchor(Path("/etc/stock-gplm"), 0, Path("/"))
_TRUST_ROOT_FIELDS = frozenset({
    "contract", "schema_version", "root_id", "generation", "status", "policy_file", "policy_sha256",
    "trusted_reviewers", "revoked_policy_sha256", "initialized_at_utc",
})


def _effective_uid() -> int:
    return os.geteuid()
_HEX = re.compile(r"^[0-9a-f]{64}$")
_REGISTRATION_FIELDS = frozenset({
    "contract", "schema_version", "registration_id", "evidence_type", "source_identity",
    "source_document_file", "source_document_sha256", "acquisition_method", "acquired_at_utc",
    "effective_start", "effective_end", "licensing_decision", "reviewer_identity",
    "approval_record", "approval_status", "evidence_schema", "evidence_version", "environment",
    "dependencies", "provenance_hash",
})
_APPROVAL_FIELDS = frozenset({
    "registration_id", "provenance_hash", "reviewer_identity", "approval_record", "status",
})


def _read_json(path: Path, maximum: int = 1024 * 1024) -> tuple[dict[str, Any], str]:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"authority input must be a bounded regular file: {path}")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("authority input must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"registered source document must be a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvidenceRegistration:
    registration_id: str
    evidence_type: str
    source_identity: str
    source_document_sha256: str
    acquisition_method: str
    acquired_at_utc: str
    effective_start: str
    effective_end: str
    licensing_decision: str
    reviewer_identity: str
    approval_record: str
    approval_status: str
    evidence_schema: str
    evidence_version: int
    environment: str
    dependencies: dict[str, str]
    provenance_hash: str
    registration_sha256: str

    def covers(self, start: str, end: str) -> bool:
        return self.effective_start <= start and end <= self.effective_end


@dataclass(frozen=True)
class ApprovalTrustPolicy:
    policy_id: str
    policy_sha256: str
    trusted_reviewers: frozenset[str]
    approvals: dict[str, dict[str, str]]
    trust_root_id: str
    trust_root_sha256: str
    trust_root_generation: int
    issuance_seal: str = field(default="", compare=False, repr=False)

    def digest(self) -> str:
        """Content identity covered by the issuance seal."""
        return canonical_hash({
            "policy_id": self.policy_id, "policy_sha256": self.policy_sha256,
            "trusted_reviewers": sorted(self.trusted_reviewers),
            "approvals": [self.approvals[key] for key in sorted(self.approvals)],
            "trust_root_id": self.trust_root_id, "trust_root_sha256": self.trust_root_sha256,
            "trust_root_generation": self.trust_root_generation,
        })

    @property
    def governed(self) -> bool:
        """True only for a policy loaded by :func:`load_trust_policy` in this process and unchanged."""
        return verify_seal("trust_policy", self.digest(), self.issuance_seal)

    def approves(self, registration: EvidenceRegistration) -> bool:
        if not self.governed:
            return False  # caller-constructed or edited policies never confer authority
        approval = self.approvals.get(registration.registration_id)
        return bool(
            registration.environment == "PRODUCTION"
            and registration.approval_status == "APPROVED"
            and registration.licensing_decision == "APPROVED_FOR_RESEARCH_USE"
            and registration.reviewer_identity in self.trusted_reviewers
            and approval == {
                "registration_id": registration.registration_id,
                "provenance_hash": registration.provenance_hash,
                "reviewer_identity": registration.reviewer_identity,
                "approval_record": registration.approval_record,
                "status": "APPROVED",
            }
        )


def load_evidence_registration(path: Path) -> EvidenceRegistration:
    value, digest = _read_json(path)
    if set(value) != _REGISTRATION_FIELDS or value["contract"] != REGISTRATION_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported evidence-registration contract or fields")
    for key in ("source_document_sha256", "provenance_hash"):
        if not isinstance(value[key], str) or _HEX.fullmatch(value[key]) is None:
            raise ValueError(f"registration {key} must be lowercase SHA-256")
    document_name = value["source_document_file"]
    if (not isinstance(document_name, str) or not document_name or Path(document_name).is_absolute()
            or len(Path(document_name).parts) != 1):
        raise ValueError("registration source_document_file must be one local filename")
    document_path = Path(path).resolve().parent / document_name
    if _file_sha256(document_path) != value["source_document_sha256"]:
        raise ValueError("registered source document bytes do not match source_document_sha256")
    if value["environment"] not in {"PRODUCTION", "SYNTHETIC"}:
        raise ValueError("registration environment must be PRODUCTION or SYNTHETIC")
    if value["approval_status"] not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("registration approval_status is unsupported")
    if value["acquisition_method"] not in {"MANUAL_BROWSER_DOWNLOAD", "MANUAL_REVIEW"}:
        raise ValueError("registration acquisition_method is unsupported")
    for key in ("evidence_type", "source_identity", "reviewer_identity", "approval_record", "evidence_schema"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"registration {key} must be a non-empty string")
    if isinstance(value["evidence_version"], bool) or not isinstance(value["evidence_version"], int) \
            or value["evidence_version"] < 1:
        raise ValueError("registration evidence_version must be a positive integer")
    try:
        acquired = datetime.fromisoformat(value["acquired_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("registration acquired_at_utc must be an ISO-8601 timestamp") from exc
    if acquired.tzinfo is None:
        raise ValueError("registration acquired_at_utc must include a timezone")
    if not isinstance(value["dependencies"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _HEX.fullmatch(v) for k, v in value["dependencies"].items()
    ):
        raise ValueError("registration dependencies must map identities to SHA-256")
    start = date.fromisoformat(value["effective_start"]).isoformat()
    end = date.fromisoformat(value["effective_end"]).isoformat()
    if start > end:
        raise ValueError("registration effective range is reversed")
    document = Path(path).resolve().parent / value["source_document_file"]
    if _file_sha256(document) != value["source_document_sha256"]:
        raise ValueError("registered source document does not match its SHA-256")
    provenance_binding = {key: value[key] for key in sorted(_REGISTRATION_FIELDS - {
        "registration_id", "provenance_hash", "approval_status", "reviewer_identity", "approval_record",
    })}
    if value["provenance_hash"] != canonical_hash(provenance_binding):
        raise ValueError("registration provenance_hash does not match its source metadata and dependencies")
    registration_binding = {key: value[key] for key in sorted(_REGISTRATION_FIELDS - {"registration_id"})}
    if value["registration_id"] != canonical_hash(registration_binding):
        raise ValueError("registration_id does not match the registration content")
    return EvidenceRegistration(
        value["registration_id"], value["evidence_type"], value["source_identity"],
        value["source_document_sha256"], value["acquisition_method"], value["acquired_at_utc"], start, end,
        value["licensing_decision"], value["reviewer_identity"], value["approval_record"],
        value["approval_status"], value["evidence_schema"], value["evidence_version"], value["environment"],
        dict(sorted(value["dependencies"].items())), value["provenance_hash"], digest,
    )


def _check_governed_stat(st: os.stat_result, what: str, anchor: TrustAnchor) -> None:
    if st.st_uid != anchor.owner_uid:
        raise ValueError(f"administrative trust {what} is not owned by the administrative account")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"administrative trust {what} must not be group/other writable")


def _read_governed_json(name: str, anchor: TrustAnchor, maximum: int = 1024 * 1024) -> tuple[dict[str, Any], str]:
    """Read one administrator-owned file from the anchor directory without following links."""
    if _effective_uid() == anchor.owner_uid:
        raise ValueError("research must not run as the administrative trust-root account")
    directory = anchor.directory
    if not directory.is_absolute() or not (directory == anchor.boundary or anchor.boundary in directory.parents):
        raise ValueError("administrative trust directory must be absolute and inside its checked boundary")
    chain = [directory, *directory.parents]
    for ancestor in chain[:chain.index(anchor.boundary) + 1]:
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"administrative trust path component is not a real directory: {ancestor}")
        _check_governed_stat(st, f"directory {ancestor}", anchor)
    descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode) or st.st_size > maximum:
            raise ValueError(f"administrative trust input must be a bounded regular file: {name}")
        _check_governed_stat(st, f"file {name}", anchor)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise ValueError(f"administrative trust input exceeds {maximum} bytes: {name}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("administrative trust input must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _unique_identities(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value) \
            or len(value) != len(set(value)):
        raise ValueError(f"{what} requires unique non-empty identities")
    return value


def _load_trust_root(anchor: TrustAnchor) -> tuple[dict[str, Any], str]:
    value, digest = _read_governed_json(TRUST_ROOT_FILE, anchor)
    if set(value) != _TRUST_ROOT_FIELDS or value["contract"] != TRUST_ROOT_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported administrative trust-root contract or fields")
    binding = {key: value[key] for key in sorted(_TRUST_ROOT_FIELDS - {"root_id"})}
    if value["root_id"] != canonical_hash(binding):
        raise ValueError("administrative trust-root identifier does not match its content")
    if value["status"] != "ACTIVE":
        raise ValueError("administrative trust root is not ACTIVE (revoked or suspended)")
    if isinstance(value["generation"], bool) or not isinstance(value["generation"], int) or value["generation"] < 1:
        raise ValueError("administrative trust-root generation must be a positive integer")
    policy_file = value["policy_file"]
    if (not isinstance(policy_file, str) or not policy_file or policy_file == TRUST_ROOT_FILE
            or Path(policy_file).is_absolute() or len(Path(policy_file).parts) != 1 or policy_file in {".", ".."}):
        raise ValueError("administrative trust root must name one policy file in its own directory")
    if not isinstance(value["policy_sha256"], str) or _HEX.fullmatch(value["policy_sha256"]) is None:
        raise ValueError("administrative trust root requires a lowercase policy SHA-256")
    _unique_identities(value["trusted_reviewers"], "administrative trust root")
    revoked = value["revoked_policy_sha256"]
    if not isinstance(revoked, list) or not all(isinstance(item, str) and _HEX.fullmatch(item) for item in revoked):
        raise ValueError("administrative trust root revoked_policy_sha256 must list SHA-256 values")
    if value["policy_sha256"] in revoked:
        raise ValueError("administrative trust root pins a revoked policy")
    try:
        initialized = datetime.fromisoformat(value["initialized_at_utc"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("administrative trust-root timestamp must be ISO-8601") from exc
    if initialized.tzinfo is None:
        raise ValueError("administrative trust-root timestamp must include a timezone")
    return value, digest


@_issues("trust_policy")
def load_trust_policy(*, _sign: Any = None) -> ApprovalTrustPolicy | None:
    """Load the policy pinned by the administrative trust root; absence leaves strict research blocked.

    Takes no path or checksum: research inputs cannot select or vouch for a policy.
    Returns ``None`` only when no trust root is installed; every other problem raises.
    """
    anchor = _ANCHOR
    try:
        os.lstat(anchor.root_path)
    except FileNotFoundError:
        return None
    root, root_digest = _load_trust_root(anchor)
    value, digest = _read_governed_json(root["policy_file"], anchor)
    if digest != root["policy_sha256"]:
        raise ValueError("active trust policy does not match the administrative trust-root SHA-256")
    if set(value) != {"contract", "schema_version", "policy_id", "trusted_reviewers", "approvals"}:
        raise ValueError("trust policy has missing or unsupported fields")
    if value["contract"] != TRUST_POLICY_CONTRACT or value["schema_version"] != 1:
        raise ValueError("unsupported trust-policy contract")
    reviewers = value["trusted_reviewers"]
    if not isinstance(reviewers, list) or not reviewers or len(reviewers) != len(set(reviewers)) or not all(
        isinstance(item, str) and item for item in reviewers
    ):
        raise ValueError("trust policy requires unique reviewer identities")
    if not set(reviewers) <= set(root["trusted_reviewers"]):
        raise ValueError("trust policy contains a reviewer not authorized by the administrative trust root")
    approvals: dict[str, dict[str, str]] = {}
    if not isinstance(value["approvals"], list):
        raise ValueError("trust policy approvals must be a list")
    for approval in value["approvals"]:
        if not isinstance(approval, dict) or set(approval) != _APPROVAL_FIELDS or approval["status"] != "APPROVED":
            raise ValueError("trust policy contains an invalid approval")
        if approval["reviewer_identity"] not in reviewers:
            raise ValueError("approval reviewer is not trusted by this policy")
        if not all(isinstance(approval[key], str) and approval[key] for key in _APPROVAL_FIELDS):
            raise ValueError("trust policy approval fields must be non-empty strings")
        if _HEX.fullmatch(approval["registration_id"]) is None or _HEX.fullmatch(approval["provenance_hash"]) is None:
            raise ValueError("trust policy approval identities must be SHA-256")
        if approval["registration_id"] in approvals:
            raise ValueError("trust policy contains duplicate registration approvals")
        approvals[approval["registration_id"]] = approval
    binding = {key: value[key] for key in ("contract", "schema_version", "trusted_reviewers", "approvals")}
    if value["policy_id"] != canonical_hash(binding):
        raise ValueError("trust-policy identifier does not match its content")
    policy = ApprovalTrustPolicy(value["policy_id"], digest, frozenset(reviewers), approvals,
                                 root["root_id"], root_digest, root["generation"])
    return replace(policy, issuance_seal=_sign(policy.digest()))
