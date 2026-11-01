# M2.5 Oracle browser timeout investigation — 2026-11-01

Follow-up to the `CANARY_FAIL` (`TIMEOUTEXCEPTION`) recorded in the
[deployment report](m25-oracle-deployment-report-2026-11-01.md). Every test was
credential-free: no login, token generation, cron/timer change or order. Browser
tests used throw-away mode-`0700` profiles, ran sequentially and were cleaned up.

## Finding

**Root cause: CPU starvation on the Oracle VM.** The VM (2 vCPU AMD EPYC 7551,
954 MiB RAM, no swap) shows **70–78 % hypervisor CPU steal** while Chrome loads
the Shoonya login page (`vmstat`, `st` column). The page is a JavaScript
single-page app (about 10 MB of bundles); its form does not exist in the initial
3.1 KB HTML. Under steal the renderer needs roughly **90 s** to build the form,
while M2.5 allowed 30 s for the page load and 30 s for the form.

Time to a usable password field (strategy `none`, 150 s bound): 88.8 s, 94.2 s,
and one earlier run of about 88 s. A pure-Python CPU benchmark on the idle VM ran
at full speed, so steal is load-triggered burst throttling, not a constant fault.

## Eliminated

| Layer | Evidence |
| --- | --- |
| DNS / TCP / TLS | 2.5 ms lookup, TLS 1.3 verified, curl loads HTTP 200 in 0.09–0.11 s |
| Page assets | all 20 assets fetch quickly with curl; net-log shows Shoonya assets done by ~10 s |
| IPv4/IPv6 | `api.shoonya.com` has no AAAA; blocking all non-Shoonya hosts did not help |
| Third-party fonts/icons | blocking them did not help |
| QUIC / proxy | no `Alt-Svc` header; no proxy environment |
| Memory / swap / `/dev/shm` | about 200 MiB peak RSS; `/dev/shm` flag not causal |
| AppArmor | exact-path profile, no `DENIED` entries |
| JavaScript enablement / throttling flags | no change |
| Page-load strategy | `none` returns instantly; the form wait itself was the long pole |
| Lean Chrome flags (no Google background services) | no material change, so not adopted |

Chrome still contacts Google update/account/autofill endpoints despite
`--disable-background-networking`; this is noise, not the cause.

## Fix (only the demonstrated cause)

[browser.py](../src/stock_gplm/m25/browser.py):

- `page_load_strategy = "none"`: do not depend on the window load event.
- `wait_for_login_form()` replaces the 30 s `WebDriverWait`. It polls for a
  displayed, enabled password input for `PAGE_READY_TIMEOUT_SECONDS` (150 s),
  retrying `WebDriverException` and urllib3 read timeouts, because ChromeDriver
  can stall beyond its 120 s HTTP read timeout while the renderer is starved.
- Expiry raises `BrowserPageNotReady` (`BROWSER_PAGE_NOT_READY`), distinct from
  `UI_CONTRACT_CHANGED`.
- `browser_timeout_seconds` now bounds the login/redirect phase and starts only
  after the form is usable; the config schema is unchanged.
- `stock-gplm-auth.service` `TimeoutStartSec` raised from 180 to 360 s so the
  unit cannot kill a slow but healthy run. This template change is not deployed.

## Verification

- Local: `.venv/bin/python -m pytest` — 187 passed; `compileall` passed.
- Oracle (credential-free, real page, real `wait_for_login_form`): form usable
  after 94.2 s, three visible inputs, no login attempted.
- Not verified: a full live login. The next canary needs separate approval and
  deployment of the updated package and unit.

## Residual risk

Steal varies with burst credits. If the render time approaches 150 s, move to a
larger shape; do not loosen the sandbox or add `--no-sandbox`.

## Independent review disposition

The 2026-11-01 Codex review rejected this revision for deployment. The CPU-steal
finding and explicit form-readiness wait remain useful, but the authentication
revision is not approved for a live canary because:

- a second credential submission can follow an ambiguous first submission;
- the current page origin is not verified before credentials are filled;
- all credential inputs and the submit control are not consistently required to
  be both visible and enabled;
- retrying a timed-out synchronous ChromeDriver command does not establish that
  the earlier operation stopped; and
- the 360-second systemd limit does not cover every permitted timeout plus
  provider exchange, validation and cleanup.

No revised package was built or deployed during that review. The authentication
service and timer must remain disabled, and live OAuth remains unapproved until
these findings are corrected, synthetically tested, packaged, and followed by
one explicitly authorized credential-free Oracle readiness test.
