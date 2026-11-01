# M2.5 Oracle offline deployment report — 2026-11-01

This report records the approved, offline deployment to the Oracle Ubuntu VM via
the Windows OpenSSH alias `sne-cloud`. It contains no credential or token values.
No live authentication, provider API request, collector change, cron change or
service/timer activation was performed.

## Release identity

- Archive: `m25-oracle-package.tar.gz`
- Bytes: `81041`
- SHA-256: `bdd582f900ecdae67dcccfb9c4b9cc050813382dd035851c46716beed2664ac9`
- Wheel SHA-256: `107881e73e7e86ff58749b7cc5da6e99a8343fe4396cf7ca68cfb970014e7cef`
- Internal manifest: all entries verified before installation
- Runtime: `/home/ubuntu/stock-gplm-auth-venv` on Python 3.12.3

## Browser runtime

Chrome for Testing and chromedriver are both version `156.0.8078.80` and are
installed under `/home/ubuntu/stock-gplm-browser/156.0.8078.80`.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Chrome ZIP | 201329937 | `b711c720f774be355197f32edf37e0651418ba036d92446ee2da19ac017540a7` |
| Chrome binary | 302710088 | `1bcad71ad1bff85955db13c6d8dd9f32374934d5a5cb079b526c46fb51f9bd97` |
| chromedriver ZIP | 11898387 | `cae58bbd262ba5c8a0ce2540f44254b6e789efc4fe9b18bbb4a00487a9f5859e` |
| chromedriver binary | 23185320 | `d7f1682dcf9e4087bae6a52ca03c9913390a79ab86e09796562db7ed6f534587` |

Ubuntu's global AppArmor user-namespace restriction remains enabled. A
root-owned exact-path profile at `/etc/apparmor.d/stock-gplm-m25-chrome` grants
only this pinned Chrome binary access to user namespaces. The browser smoke test
passed with Chrome's sandbox enabled; `--no-sandbox` was not used.

## Installed but inactive controls

- Non-secret config: `/etc/stock-gplm/m25-auth.json`, mode `0640`, root:ubuntu
- AppArmor profile: mode `0644`, root:root
- Offline preflight unit: static and inactive
- Authentication service: disabled and inactive
- Authentication timer: disabled and inactive

`systemd-analyze verify` passed for all three units. The reported security
exposure was `4.2 OK` for preflight and `4.5 OK` for authentication.

## Offline verification

- `pip check`: passed
- CLI preflight: passed with NTP synchronized, Selenium `4.52.0`, matched browser
  and driver, `network_attempted=false`, `login_attempted=false`
- Selenium data-URL smoke: passed with browser version `156.0.8078.80` and sandbox
  enabled
- No-login state-machine smoke: produced the expected `LOGIN_REQUIRED`, created
  no token cache and did not start the browser or use the network
- Local synthetic suite before transfer: 163 tests passed

## Remaining approval and credential blockers

At the time of the initial deployment, the encrypted systemd credential directory
did not exist. Existing protected Oracle secret files were not read, copied or
transformed in that initial pass. The later credential-readiness pass recorded
below completed the Oracle-local encrypted provisioning and offline preflight.

Before a separately approved live canary, confirm the registered redirect host,
the Oracle static-IP registration and the account identifier binding. A mismatch
fails closed. The first live login, timer activation, collector wrapper and cron
changes remain unapproved.

## Credential-readiness pass — 2026-11-01

The approved readiness pass connected through `sne-cloud` and kept all secret
material on Oracle. No password, TOTP seed, API secret or token value was printed,
copied to the development machine or placed on a command line.

### Final readiness release

- Archive: `m25-oracle-package-readiness-final-20261101.tar.gz`
- Archive SHA-256: `4470699b66b5bbc1c3bf47eed4b0084598ee36f6f2be6ea868d8b863dc596acf`
- Wheel SHA-256: `ea5d64b61e1ad93fc8e435f0e55336846839843ad6c86bc5e96d05196fd05e4e`
- Internal manifest: all entries verified on Oracle
- Local complete suite: `170 passed in 0.65s`
- Local `compileall`: passed

Three existing mode-`0600` Oracle-local legacy sources were validated without
displaying their values and converted locally into named systemd encrypted
credentials. The encrypted store is root-owned mode `0700`; each encrypted blob
is root-owned mode `0600`. The original protected sources were left unchanged.

The first service preflight exposed a compatibility error: systemd presents
decrypted credentials inside the unit as a root-owned read-only credential mount
(`0550` directory and `0440` files). The validator previously accepted only
`0700`/`0600`. The implementation now accepts only systemd's root/root or
root/service-group read-only pattern while continuing to reject group writes,
world access, unexpected groups, symlinks and unsafe ownership. The corrected
package passed the complete synthetic suite and was checksum-verified before and
after transfer.

The real offline systemd preflight then passed with credentials reported only as
`VALIDATED_NOT_EXPOSED`, NTP synchronized, matched Chrome/chromedriver, Selenium
`4.52.0`, `network_attempted=false` and `login_attempted=false`. A separate
`data:` URL Selenium smoke test passed with Chrome's sandbox enabled. The current
local token cache has matching configured UID/account metadata and was locally
age-eligible during the check; no server validation was attempted.

The authentication service and timer remain disabled and inactive. No collector
wrapper or cron change was made. The live authentication service was not started.

### Remaining blockers

The configuration, legacy collector identity and cached UID/account metadata are
internally consistent without exposing their values. This is not provider-side
proof that Shoonya currently binds the client to the intended account.

Oracle instance metadata exposes the private VNIC but no public-IP allocation or
reserved/static status, and no OCI CLI/SDK or local registration evidence is
available. The Shoonya-registered static egress IP therefore remains unverified.
Likewise, the configured HTTPS redirect allowlist is syntactically valid, but no
authoritative Shoonya-side redirect-registration evidence is locally available.

The VM has no TPM2. `systemd-creds` used its root-protected host key and warned
that the host key file is not on a filesystem it recognizes as encrypted. OCI
volume-level encryption was not independently established during this pass.

Status: `BLOCKED_FOR_LIVE_CANARY`. Obtain authoritative Shoonya confirmation of
the registered static IP, redirect host and client/account binding, and accept or
remediate the host-key-at-rest caveat before approving the first live canary.

## Final OAuth canary — 2026-11-01

The operator confirmed that Shoonya's registered IP and Oracle's observed
outbound IP are both `130.210.5.183`, and that the registered redirect is exactly
`http://127.0.0.1`.

M2.5 was changed to accept only the portless root-path loopback callback on
`127.0.0.1`. Alternate loopback names/addresses, explicit ports, non-root paths,
additional or duplicate query parameters, fragments and user information are
rejected. Non-loopback callbacks retain HTTPS enforcement, an allowlisted host
and the approved `/OAuthlogin/authorize/oauth` path. Selenium page loading is
bounded before navigating to the login page, and provider SDK stdout/stderr is
discarded so token-bearing responses cannot enter service logs.

Verification before transfer completed with `183 passed in 0.68s` and
`compileall` passing. The deployed final release was:

- Archive: `m25-oracle-canary-final-20261101.tar.gz`
- Archive SHA-256: `6444fe1b0a584b06a880c7c7f084fd470677ee2ab7ceb06e56d83728392e1d7f`
- Wheel SHA-256: `7a6bf4ad5298e0cf0c28981e728586d217c982baecffeaeabbbd7f469c75d1d0`
- Internal manifest, `pip check`, Oracle parser test and Selenium-log capture test:
  passed
- Final encrypted-credential offline preflight and sandboxed `data:` browser
  smoke: passed

Exactly one authorized live canary was started as a bounded transient systemd
unit with separate mode-`0700` state. The authentication service and timer were
not enabled or started, and no collector ran. The audit sequence was
`AUTH_STARTED`, `AUTH_LOCK_ACQUIRED`, `CACHE_MISSING`, `OAUTH_BROWSER_STARTED`,
`AUTH_FAILED` with outcome `TIMEOUTEXCEPTION`.

Chrome timed out loading the hosted Shoonya login page before the login form was
available. No authorization code was captured, no token exchange or read-only
session validation occurred, and no canary token cache was created. Chrome and
chromedriver exited. No retry was performed. No cron, timer, collector or order
action occurred.

Canary result: `CANARY_FAIL`.

## Canary failure follow-up

The `CANARY_FAIL` was investigated credential-free; the cause was CPU steal making
the login form take about 90 s to render, against a 30 s bound. See the
[browser timeout investigation](m25-oracle-browser-timeout-investigation-2026-11-01.md).
The fix is in the repository but not deployed; the live canary remains unapproved.
