# M5.1.1 evidence authority and registered market assumptions

M5.1.1 separates byte integrity from authority. A local checksum proves only
that bytes did not change. A registration's own filename, reviewer fields or
`approval_status` never authorize strict research.

## Trust boundary

`stock-gplm.m511-evidence-registration.v1` records the evidence type and source
identity, actual source-document filename and SHA-256, manual acquisition method
and timestamp, effective range, licensing decision, reviewer and approval
record, approval status, evidence schema/version, environment, dependency hashes
and a canonical provenance hash. The loader verifies the actual source-document
bytes and every identity hash.

Authority additionally requires a separate
`stock-gplm.m511-approval-trust-policy.v1`. Its file SHA-256 must be supplied to
`load_reference_evidence` out of band together with the policy path. The policy
must trust the named reviewer and contain an exact approval for the
registration/provenance/approval-record tuple. A `SYNTHETIC` registration can
never be approved, even when a policy lists it. No trust policy or production
approval is included in this repository.

An approved `REFERENCE_EVIDENCE_BUNDLE` registration must bind the exact hashes
of every loaded dependency. It lifts `EVIDENCE_AUTHORITY_UNAPPROVED` only for its
effective range. Legacy source strings are still scanned as a fail-closed
secondary check, but the structured `environment` and external trust anchor are
the primary controls for new registrations.

## Instrument classification

`stock-gplm.m511-instrument-classification.v1` contains an effective-dated,
canonical-hash-bound list of `EQUITY`/`ETF` classifications. Its separate
registration must bind the payload and be approved by the trust policy.
Strict assessment requires coverage for the requested symbol and complete date
range. An authoritative `ETF` is rejected. If the source is missing,
unapproved, out of range or omits the symbol, the M3.7 naming heuristic remains
diagnostic information only and strict mode stays blocked.

## Cost schedule

`stock-gplm.m511-cost-schedule.v1` states all four nonnegative components:
commission, slippage, buy statutory charges and sell statutory charges, in bps,
with effective dates and a canonical profile ID. Zero is allowed only when the
approved payload explicitly states zero. Strict execution requires every
dataset to bind the same approved profile, requires strategy/settings component
values to equal it exactly, and requires `settings.cost_basis` to equal the
profile ID. Diagnostic runs may continue to use explicitly labelled assumptions.

## Availability

The loaders and fail-closed policy are implemented. Genuine approved source
documents, reviewer approvals, an out-of-band trust-policy hash, authoritative
classification and a verified cost schedule are not available, so strict
real-data research remains `BLOCKED`.
