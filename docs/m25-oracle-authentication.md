# M2.5 Oracle-native unattended Shoonya authentication

M2.5 obtains and renews a Shoonya OAuth session entirely on the Oracle Ubuntu VM.
The WSL development machine builds and tests the package but is never a runtime
dependency. M2.5 does not place orders, change provider data, alter raw collector
files, or bypass CAPTCHA and other interactive controls.

One explicitly authorized 2026-11-01 canary opened the hosted page but timed out
before the form appeared; it did not submit credentials or obtain a token. The
M2.5.1 safety remediation is synthetic-only and has not been deployed or tested
with credentials. Historical evidence is recorded in
`m25-oracle-deployment-report-2026-11-01.md`.

## Provider contract

The implementation follows Shoonya's documented Selenium OAuth flow:

1. launch headless Chrome;
2. require the exact `https://api.shoonya.com` origin, expected login path,
   Shoonya title marker, three-input type contract and one `LOGIN` button;
3. require all three inputs and the button to be visible, enabled and
   interactable, then submit User ID, password and a fresh TOTP exactly once;
4. capture a bounded authorization code from the exact registered
   `http://127.0.0.1` loopback redirect, or a narrowly approved HTTPS redirect;
5. close Chrome;
6. exchange the one-time code with `NorenApi.getAccessToken`; and
7. validate and cache the resulting token.

References:

- <https://www.shoonya.com/api-documentation/python-authentication-selenium>
- <https://www.shoonya.com/api-documentation/auth-overview>
- <https://www.shoonya.com/api-documentation/token-renewal>
- <https://www.shoonya.com/api-documentation/ip-whitelisting>

Shoonya documents a roughly 24-hour token and no refresh endpoint. M2.5 uses a
23-hour local ceiling plus a read-only server validation. It never reuses an OAuth
code. A CAPTCHA, password-reset page, consent change, unexpected DOM, wrong
redirect origin, explicit redirect port, unexpected path/query parameter or
unknown challenge stops as an operator-visible failure.

## Integration with the existing collector

The production collector continues to call its existing `shoonya_session.get_api`
and retains its existing publication behavior. M2.5 writes the same protected
`~/.config/stock-gplm/shoonya_token.json` cache. The V1 cache includes the legacy
`created_at`, `token`, `uid` and `account_id` fields, so the current session loader
can consume it without code changes.

The proposed `authenticated-collector.sh` wrapper runs M2.5 first, then `exec`s
the existing `run_scheduled.sh`. The current collector lock, calendar logic,
provider reads, immutable-file checks and marker-last publication remain intact.
Changing cron to use the wrapper is a separate approval gate.

M2.5's own `ReadOnlyShoonyaClient` exposes only `get_limits`,
`get_daily_price_series` and `get_time_price_series`. It exposes no order method.

## Session decisions

Under the exclusive `shoonya_auth.lock`, M2.5 re-reads the cache and applies:

| Evidence | Decision |
| --- | --- |
| young cache plus `get_limits` `stat=Ok` | reuse; no browser |
| explicit invalid/expired session | one fresh OAuth flow if explicitly enabled |
| timeout, DNS/TLS error or unknown provider response | preserve cache and fail closed |
| missing/locally expired cache | fresh flow only with `--allow-oauth-login` |
| UID/account mismatch | security failure; do not cache |
| CAPTCHA, changed login identity, disabled control or unexpected origin | fail closed before submission |
| timeout or transport error during/after click | ambiguous outcome; destroy browser process group; never resubmit |

An authentication attempt has the linear states `STARTING`, `PAGE_VERIFIED`,
`CREDENTIALS_FILLED`, `SUBMITTED` and `CODE_OBSERVED`. The transition into
`SUBMITTED` increments a guarded submission counter from zero to one before the
click command begins. There is no credential resubmission, including after a
TOTP rollover, timeout, missing redirect or ambiguous click result.

## Security boundaries

- Static secrets are read from three fixed systemd credential filenames; values
  never appear on argv, in JSON config, or in logs.
- Credential directories/files must be owned by root or the service UID and must
  deny group/world access.
- State paths are absolute and explicitly forbidden inside daily/minute raw roots.
- Cache writes use a same-directory temporary file, mode `0600`, file `fsync`,
  atomic replace and directory `fsync`.
- The persistent lock inode uses `flock`; process death releases the kernel lock.
- Chrome runs as non-root with its sandbox enabled. M2.5 never supplies
  `--no-sandbox` and never persists page source, screenshots or performance logs.
- On Ubuntu releases that restrict unprivileged user namespaces, downloaded
  Chrome uses a root-owned AppArmor profile bound to its exact canonical binary
  path. The global user-namespace restriction remains enabled.
- The browser uses an ephemeral mode-`0700` profile and accepts authorization
  codes only from the exact portless root-path `http://127.0.0.1` callback or a
  configured HTTPS host using `/OAuthlogin/authorize/oauth`. Redirects with
  additional parameters, fragments, user information or explicit ports fail
  closed.
- The exact login-page identity and complete control set are checked with one
  browser-side predicate. Credential filling and the single click each repeat
  that origin/page/control predicate atomically in the browser context.
- Selenium and Chrome run in a dedicated process group. A stalled WebDriver
  command is never retried. The parent terminates the whole group, first with
  `SIGTERM` and then `SIGKILL` within the cleanup allowance, so no later command
  targets an unresolved session.
- The hosted login form can take about 90 s to render on the CPU-throttled Oracle
  VM. Page-load strategy `none` and a 150 s complete-form wait remain bounded;
  `browser_timeout_seconds` is capped at 90 s for fill, one submission and redirect.
  See the
  [investigation](m25-oracle-browser-timeout-investigation-2026-11-01.md).
- Every Shoonya SDK HTTP POST has an explicit `(5 s connect, 15 s read)` timeout.
  The whole authentication operation has an independent 360 s real-time deadline.
- Audit and status files are mode `0600`; sensitive field names are redacted.
- Core dumps are disabled by the service templates.

## Complete timeout budget

The maximum configured path is bounded as follows. The 360 s application
deadline is authoritative if phase allowances do not all fit because of local
overhead.

| Phase | Maximum |
| --- | ---: |
| Exclusive authentication lock | 30 s |
| ChromeDriver/Chrome startup | 30 s |
| Exact page and complete-control readiness | 150 s |
| TOTP alignment, one submission and redirect observation | 90 s |
| Browser process-group cleanup | 10 s |
| Provider token exchange HTTP connect/read | 5 s / 15 s |
| Read-only provider validation HTTP connect/read | 5 s / 15 s |
| Whole application authentication attempt | 360 s |
| systemd `TimeoutStartSec` | 390 s |
| systemd termination allowance (`TimeoutStopSec`) | 30 s |

The service start ceiling leaves 30 s beyond the application deadline for CLI,
audit/status and bounded cleanup. `KillMode=control-group` and `SendSIGKILL=yes`
cover any remaining descendants. The worst systemd-enforced failure envelope is
420 s (390 s start ceiling plus at most 30 s termination).

## CLI

The local/offline commands are:

```bash
stock-gplm-auth preflight \
  --config /etc/stock-gplm/m25-auth.json \
  --credentials-dir /protected/credential/directory

stock-gplm-auth status \
  --config /etc/stock-gplm/m25-auth.json
```

`preflight` validates configuration, credential files, NTP, Selenium, Chrome and
chromedriver without opening a network connection or logging in. `status` reads
only nonsensitive local cache metadata.

The production-capable command is deliberately explicit:

```bash
stock-gplm-auth ensure \
  --config /etc/stock-gplm/m25-auth.json \
  --allow-oauth-login
```

Without `--allow-oauth-login`, an absent or expired token produces
`LOGIN_REQUIRED` and Chrome is not launched.

## Build in WSL

```bash
cd /home/mgurunath/projects/stock-gplm
.venv/bin/python -m pytest
.venv/bin/python -m pip wheel --no-deps . --wheel-dir dist/m25
```

The deployment payload is `deploy/oracle/m25/` plus the generated wheel. Compute
and record SHA-256 values before any approved transfer.

## Oracle deployment procedure

Every step below requires deployment approval. The initial 2026-11-01 deployment
stopped before credential provisioning. The later credential-readiness pass
provisioned host-encrypted systemd credentials and passed the offline preflight,
but still stopped before live login, authentication-service activation and
scheduling. See the deployment report for the recorded evidence and blockers.

1. After transfer approval, copy only the checksum-bound package. Do not copy
   credentials from WSL:

   ```bash
   /mnt/c/Windows/System32/OpenSSH/scp.exe \
     dist/m25-oracle-package.tar.gz \
     sne-cloud:/home/ubuntu/m25-oracle-package.tar.gz
   ```

2. On Oracle, verify the outer digest supplied with the release, extract into a
   new private staging directory, and verify every internal file:

   ```bash
   sha256sum /home/ubuntu/m25-oracle-package.tar.gz
   install -d -m 0700 /home/ubuntu/m25-staging
   tar --no-same-owner -xzf /home/ubuntu/m25-oracle-package.tar.gz \
     -C /home/ubuntu/m25-staging
   cd /home/ubuntu/m25-staging/m25-oracle-package
   sha256sum --check MANIFEST.sha256
   ```
3. Provision a signed Chrome/Chromium package and matching chromedriver. Record
   exact versions and paths; do not rely on an unpinned automatic download.
4. Create a dedicated authentication environment:

   ```bash
   python3.12 -m venv /home/ubuntu/stock-gplm-auth-venv
   /home/ubuntu/stock-gplm-auth-venv/bin/python -m pip install \
     --requirement deploy/oracle/m25/requirements.lock
   /home/ubuntu/stock-gplm-auth-venv/bin/python -m pip install \
     --no-deps dist/m25/stock_gplm-*.whl
   ```

5. Copy `auth-config.example.json` to `/etc/stock-gplm/m25-auth.json`, replace only
   identity and binary-path placeholders, then protect it:

   ```bash
   sudo install -d -o root -g ubuntu -m 0750 /etc/stock-gplm
   sudo install -o root -g ubuntu -m 0640 \
     deploy/oracle/m25/auth-config.example.json \
     /etc/stock-gplm/m25-auth.json
   sudoedit /etc/stock-gplm/m25-auth.json
   ```
6. Confirm Oracle's static egress IP is the IP registered for the Shoonya Client
   ID and confirm the registered redirect host. Add that exact host to
   `allowed_redirect_hosts` if it is not already listed.
7. Create encrypted systemd credentials interactively on Oracle; never pass their
   values on a command line:

   ```bash
   sudo install -d -m 0700 /etc/credstore.encrypted/stock-gplm
   sudo systemd-creds encrypt --name=shoonya_password - \
     /etc/credstore.encrypted/stock-gplm/shoonya_password
   sudo systemd-creds encrypt --name=shoonya_totp_secret - \
     /etc/credstore.encrypted/stock-gplm/shoonya_totp_secret
   sudo systemd-creds encrypt --name=shoonya_api_secret - \
     /etc/credstore.encrypted/stock-gplm/shoonya_api_secret
   ```

8. With deployment approval, install only the offline preflight unit and run it.
   It has no IP network address family and cannot perform a login:

   ```bash
   sudo install -o root -g root -m 0644 \
     deploy/oracle/m25/stock-gplm-auth-preflight.service \
     /etc/systemd/system/stock-gplm-auth-preflight.service
   sudo systemctl daemon-reload
   sudo systemctl start stock-gplm-auth-preflight.service
   sudo systemctl status --no-pager stock-gplm-auth-preflight.service
   sudo journalctl -u stock-gplm-auth-preflight.service --no-pager -n 100
   ```

9. Stop for supervised-live-login approval. Only after approval:

   ```bash
   sudo install -o root -g root -m 0644 \
     deploy/oracle/m25/stock-gplm-auth.service \
     /etc/systemd/system/stock-gplm-auth.service
   sudo systemctl daemon-reload
   sudo systemctl start stock-gplm-auth.service
   sudo systemctl status --no-pager stock-gplm-auth.service
   ```

   Inspect only the redacted status/audit and verify the existing collector reuses
   the new cache without invoking Selenium again.

10. Stop again for scheduling approval. The following commands are examples for
    that later approval and must not be run now:

    ```bash
    sudo install -o root -g root -m 0644 \
      deploy/oracle/m25/stock-gplm-auth.timer \
      /etc/systemd/system/stock-gplm-auth.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now stock-gplm-auth.timer
    ```

    Installing `authenticated-collector.sh` or changing the current cron entry is
    a separate collector-scheduling change and is not part of authentication
    deployment approval.

## Monitoring and recovery

Audit events include cache hit/expiry, lock acquisition, browser start, code
capture as a boolean, cache commit and stable failure class. They never include
credentials, tokens, codes, checksums, redirect URLs, page content or raw provider
responses.

Key outcomes are `VALID_REUSED`, `REFRESHED`, `LOGIN_REQUIRED`,
`VALIDATION_INCONCLUSIVE`, `INTERACTION_REQUIRED`, `UI_CONTRACT_CHANGED`,
`AUTH_OUTCOME_AMBIGUOUS`, `BROWSER_SESSION_UNRESOLVED`, `PROVIDER_TIMEOUT`,
`OPERATION_DEADLINE_EXCEEDED`, `TOKEN_EXCHANGE_REJECTED` and
`SECURITY_BOUNDARY_ERROR`.

On rollback, disable any M2.5 units, restore the previous authenticated collector
entrypoint, and leave the last protected token cache untouched. Do not delete raw
publications. An explicit Shoonya logout is a separate security action only when
a token is suspected compromised.

## Remaining approval gates and open inputs

- Approved Chrome/chromedriver source and exact matched versions.
- Oracle static-IP registration confirmation.
- Registered redirect-host confirmation.
- Supervised verification of the current login DOM and selectors.
- Alert destination and retention policy.
- Separate approval for one live login.
- Separate approval for systemd timer or cron changes.
