# M2.5 Oracle deployment payload

This directory is an inert deployment template. Copying files, installing browser
packages, storing credentials, enabling units, changing cron and running `ensure`
all require separate production approval.

Contents:

- `requirements.lock`: exact Python dependency resolution for Python 3.12;
- `auth-config.example.json`: non-secret Oracle path and identity contract;
- `stock-gplm-auth-preflight.service`: offline prerequisite validation template;
- `stock-gplm-auth.service`: disabled hardened systemd oneshot template;
- `stock-gplm-auth.timer`: disabled pre-market timer template;
- `stock-gplm-chrome.apparmor.in`: exact-path user-namespace profile template
  for downloaded Chrome on Ubuntu 23.10 and later;
- `authenticated-collector.sh`: proposed authentication-before-collection wrapper.

Use `docs/m25-oracle-authentication.md` for staged installation, validation,
rollback and approval gates. Never put credential values in the JSON config,
shell scripts, command line, repository or logs.

Do not use Chrome's `--no-sandbox` option. When Ubuntu restricts unprivileged
user namespaces, render `stock-gplm-chrome.apparmor.in` with the canonical,
root-controlled Chrome binary path, install the result under `/etc/apparmor.d/`
as root, and reload only that profile.
