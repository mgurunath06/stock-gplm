# M2.5.1 authentication safety remediation — 2026-11-01

This revision corrects the seven independent-review findings without using any
credential, TOTP, live Shoonya login, Oracle connection, deployment, collector,
scheduler or production change. All authentication evidence is synthetic.

## Corrections and acceptance criteria

| Finding | Correction | Acceptance criterion |
| --- | --- | --- |
| Duplicate submission after ambiguity | Linear state machine advances a guarded counter to one before the click begins. Any click/observation timeout or absent redirect is `AUTH_OUTCOME_AMBIGUOUS`. | Every attempt executes zero or one submit command; adversarial click timeout and missing-code tests observe exactly one. |
| Unverified login destination | Login is fixed to `https://api.shoonya.com/OAuthlogin/investor-entry-level/login`. Origin, portless path, title marker and input/button identity are checked before credentials and again inside fill/click scripts. | Unexpected origin/path/title/control identity fails before fill or click. |
| Partial control readiness | All three expected typed inputs and the sole `LOGIN` button must be present, visible, enabled, non-read-only, pointer-enabled, in the viewport, unobscured at center and have nonzero geometry. | A disabled/noninteractable input or button prevents all credential entry. |
| Overlapping WebDriver commands | Selenium owns a separate Linux process group. A watchdog observes both worker death and parent lifecycle-pipe EOF. A two-way handshake prohibits Selenium startup until `setsid()` and `pid == pgid` are parent-verified. Completed not-ready polls may repeat; any unresolved command abandons the group and never issues another WebDriver command. | Real Linux subprocess tests prove worker-exit cleanup, parent cleanup of a SIGTERM-resistant descendant, parent death after readiness, and parent death before `setsid()`. Cleanup verifies the process group no longer exists. |
| Incomplete wall-clock bounds | The browser worker bounds startup, readiness, authentication and cleanup. Provider POSTs force explicit connect/read timeouts. A POSIX real-time timer caps the whole authentication attempt. | Phase constants, provider tuple and the 360 s application deadline are asserted by tests. |
| Undersized systemd timeout | `TimeoutStartSec=390`, `TimeoutStopSec=30`, `KillMode=control-group`, and `SendSIGKILL=yes`. | The start ceiling exceeds the 360 s application bound by 30 s and descendants remain in the kill scope. |
| Security-control regression risk | No `--no-sandbox`; AppArmor template, encrypted `LoadCredentialEncrypted`, unit hardening, cache/audit permissions and sensitive-field redaction are retained. | Regression suite covers redaction/credential permissions; source check rejects `--no-sandbox`; unit controls remain present. |

## Exact timeout budget

- Lock: at most 30 s.
- Browser: at most 280 s total, comprising a 5 s process-group/watchdog
  handshake, 25 s ChromeDriver startup, 150 s complete-page readiness, 90 s
  single-submission/redirect phase, and 10 s cleanup allowance.
- Provider exchange: explicit 5 s connect and 15 s read timeout.
- Provider validation: explicit 5 s connect and 15 s read timeout.
- Whole application authentication attempt: hard 360 s real-time deadline. This
  wins over the sum of phase limits if local overhead consumes the remaining time.
- systemd: 390 s start ceiling, followed by at most 30 s termination allowance;
  absolute failure envelope 420 s.

## Verification required for acceptance

The change is accepted for independent review only when the complete pytest
suite, `compileall`, and Gitleaks pass; the feature commit is pushed; and a draft
pull request targets `main`. Passing these checks does not authorize deployment
or live authentication.

Local verification on the feature worktree:

- `.venv/bin/python -m pytest`: **202 passed**.
- `.venv/bin/python -m compileall -q src tests`: **passed**.
- Gitleaks **8.30.1**, downloaded from the official release and matched against
  its published checksum: `git` scan **no leaks found**; `dir` scan including
  uncommitted files **no leaks found**.
- `systemd-analyze verify` parsed the unit files but could not complete on WSL
  because the Oracle-only `/home/ubuntu/stock-gplm-auth-venv/bin/stock-gplm-auth`
  executable is intentionally absent. This is not claimed as a passing check;
  the unit must be re-verified in an approved Oracle offline preflight.
- A credential-free HTTPS GET to the exact login path, with no query parameters
  and redirects forbidden, returned the public bootstrap HTML with
  `<title>SHOONYA</title>`. No credential entry or authentication was attempted.

## Residual risks

- The public bootstrap title is verified, but the rendered Shoonya control DOM
  and JavaScript value-setter compatibility are not live-verified. A provider
  change fails closed and needs a separately authorized credential-free browser
  readiness check before any canary.
- A browser can receive credentials and then fail before the redirect is observed.
  The result remains intentionally ambiguous and requires operator investigation;
  automated retry is forbidden.
- POSIX `SIGALRM`, `fork`, process groups and group signals make this Linux-only.
- The guarantee covers Chrome/ChromeDriver descendants that remain in the
  worker's inherited process group. A future browser/driver version that
  deliberately creates a new session or moves itself to another process group
  would require a cgroup-level assertion during the Oracle offline preflight;
  systemd `KillMode=control-group` remains the outer deployment safeguard.
- Requests connect/read timeouts are not authenticity proof and a peer can extend
  a read by continuously delivering bytes. The independent 360 s deadline is the
  final wall-clock bound.
- Host-key-at-rest, static-IP, redirect registration and account binding evidence
  remain deployment gates. This revision does not establish production readiness.
