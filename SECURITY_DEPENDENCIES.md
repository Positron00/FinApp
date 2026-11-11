# Accepted dependency and image findings

The CI gates fail on any HIGH or CRITICAL image CVE (Trivy), any Python advisory (pip-audit
`--strict`) and any JS advisory at `high` or above (`pnpm audit --audit-level high`) (platform LLD
§12; PAY-NFR-12). A finding may be accepted only when it can't be fixed yet. Each acceptance is
recorded here **and** in the tool's ignore list, with a reason, an owner and an expiry date:

| Tool       | Ignore list                                                                                   | Checked by                                                                                                  |
| ---------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Trivy      | `.trivyignore`: `<ID> # reason: <text>; owner: <github handle>; exp:YYYY-MM-DD`               | `scripts/ci/images.sh` policy step (C38): fails on a missing field, a past expiry or an ID not listed below |
| pip-audit  | the **pip-audit** table below (`--ignore-vuln <ID>`)                                          | `scripts/ci/security.sh` reads the IDs from this file; expired rows fail                                    |
| pnpm audit | `pnpm.auditConfig.ignoreGhsas` in the root `package.json`, listed in the **pnpm** table below | `scripts/ci/security.sh`                                                                                    |

When an expiry passes, the gate fails again: fix the finding (upgrade, rebuild on a patched base
image) or renew the acceptance with a fresh review. The nightly `audit` job re-runs all three
against fresh advisory databases.

## Trivy (images)

Accepted on 2026-11-10 (task S9) after the first full image scan. Every entry is a Debian package of
the base image (`python:3.14.9-slim-trixie` for the four Python services, `node:24.22.0-slim`, which
is Debian 12, for the web) for which Debian has no fixed version. Findings that do have a fix (apt,
zlib in Debian 13, pip's and npm's vendored packages) are not accepted: the Dockerfiles take the
Debian updates and drop pip, npm and corepack from the runtime images.

| ID              | Package / image                                                                                                                                                   | Severity | Reason                                                                                                                                                                    | Owner       | Expires    |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ---------- |
| CVE-2023-45853  | zlib1g (web (Debian 12))                                                                                                                                          | CRITICAL | will_not_fix: no Debian 12 fix: node statically links its own zlib, so the web server never loads system zlib1g (fixed in Debian 13: move the web image to a trixie base) | @Positron00 | 2026-12-10 |
| CVE-2025-69720  | libncursesw6, libtinfo6, ncurses-base, ncurses-bin (python services (Debian 13); web (Debian 12))                                                                 | HIGH     | affected: no Debian fix yet: terminal library of the base image, unused by a non-interactive server                                                                       | @Positron00 | 2027-01-10 |
| CVE-2026-119373 | libsqlite3-0 (python services (Debian 13))                                                                                                                        | HIGH     | affected: no Debian 13 fix yet: the services never import sqlite3 and use neither FTS5 nor session changesets                                                             | @Positron00 | 2027-01-10 |
| CVE-2026-119374 | libsqlite3-0 (python services (Debian 13))                                                                                                                        | HIGH     | affected: no Debian 13 fix yet: the services never import sqlite3 and use neither FTS5 nor session changesets                                                             | @Positron00 | 2027-01-10 |
| CVE-2026-119379 | libsqlite3-0 (python services (Debian 13))                                                                                                                        | HIGH     | affected: no Debian 13 fix yet: the services never import sqlite3 and use neither FTS5 nor session changesets                                                             | @Positron00 | 2027-01-10 |
| CVE-2026-16742  | libsystemd0, libudev1 (python services (Debian 13); web (Debian 12))                                                                                              | HIGH     | affected, fix_deferred: no Debian fix yet: systemd-homed is not present or run in the container, only the client libraries are installed                                  | @Positron00 | 2027-01-10 |
| CVE-2026-41992  | gzip (web (Debian 12))                                                                                                                                            | HIGH     | fix_deferred: Debian fix deferred: the gzip binary is never run by the web server                                                                                         | @Positron00 | 2027-01-10 |
| CVE-2026-53613  | bsdutils, libblkid1, libmount1, libsmartcols1, libuuid1, mount, util-linux, util-linux-extra (web (Debian 12))                                                    | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-53615  | bsdutils, libblkid1, libmount1, libsmartcols1, libuuid1, mount, util-linux, util-linux-extra (web (Debian 12))                                                    | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-54369  | libacl1 (python services (Debian 13); web (Debian 12))                                                                                                            | HIGH     | affected, fix_deferred: no Debian fix yet: libacl is not used by the services, which run non-root on a read-only root filesystem                                          | @Positron00 | 2027-01-10 |
| CVE-2026-76642  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services (Debian 13))                                    | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-76844  | zlib1g (web (Debian 12))                                                                                                                                          | HIGH     | affected: no Debian 12 fix: node statically links its own zlib, so the web server never loads system zlib1g (fixed in Debian 13: move the web image to a trixie base)     | @Positron00 | 2026-12-10 |
| CVE-2026-78408  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services (Debian 13))                                    | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-78409  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services (Debian 13))                                    | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-78410  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux, util-linux-extra (python services (Debian 13); web (Debian 12)) | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                          | @Positron00 | 2027-01-10 |
| CVE-2026-85091  | zlib1g (web (Debian 12))                                                                                                                                          | HIGH     | affected: no Debian 12 fix: node statically links its own zlib, so the web server never loads system zlib1g (fixed in Debian 13: move the web image to a trixie base)     | @Positron00 | 2026-12-10 |
| CVE-2026-9538   | perl-base (python services (Debian 13); web (Debian 12))                                                                                                          | HIGH     | fix_deferred: Debian fix deferred: perl Archive::Tar is never run in the containers                                                                                       | @Positron00 | 2027-01-10 |

## pip-audit (Python)

| ID  | Package | Reason | Owner | Expires |
| --- | ------- | ------ | ----- | ------- |

None accepted.

## pnpm audit (JavaScript)

| ID  | Package | Reason | Owner | Expires |
| --- | ------- | ------ | ----- | ------- |

None accepted.
