# Accepted dependency and image findings

The CI gates fail on any HIGH or CRITICAL image CVE (Trivy), any Python advisory (pip-audit
`--strict`) and any JS advisory at `high` or above (`pnpm audit --audit-level high`) (platform LLD
§12; PAY-NFR-12). A finding may be accepted only when it can't be fixed yet. Each acceptance is
recorded here **and** in the tool's ignore list, with a reason, an owner and an expiry date:

| Tool       | Ignore list                                                                                   | Checked by                                                                                                  |
| ---------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Trivy      | `.trivyignore`: `<ID> # reason: <text>; owner: <github handle>; exp:YYYY-MM-DD`               | `scripts/ci/images.sh` policy step (C38): fails on a missing field, a past expiry or an ID not listed below |
| pip-audit  | the **pip-audit** table below (`--ignore-vuln <ID>`)                                          | `scripts/ci/security.sh` reads the IDs from this file; expired rows fail                                    |
| pnpm audit | `pnpm.auditConfig.ignoreGhsas` in the root `package.json`, listed in the **pnpm** table below | `scripts/ci/security.sh` runs the audit; the expiry is **not** checked automatically, so review it by hand  |

When an expiry passes, the Trivy and pip-audit gates fail again (pnpm acceptances have no automatic
expiry: none exist today; add an expiry check to `security.sh` before accepting one): fix the finding (upgrade, rebuild on a patched base
image) or renew the acceptance with a fresh review. The nightly `audit` job re-runs all three
against fresh advisory databases.

## Trivy (images)

Accepted on 2026-11-10 (task S9) after the first full image scan; reviewed on 2026-11-11 (C78), when
the web image moved from Debian 12 (`node:24.22.0-slim`) to Debian 13 (`node:24.22.0-trixie-slim`), like
the Python images (`python:3.14.9-slim-trixie`). That removed six findings, including the only CRITICAL one
(zlib CVE-2023-45853, CVE-2026-76844 and CVE-2026-85091; gzip CVE-2026-41992; util-linux CVE-2026-53613
and CVE-2026-53615). All five images now carry the same 11 HIGH findings, each a Debian 13 package for which
Debian has no fixed version. Findings that do have a fix (apt,
zlib in Debian 13, pip's and npm's vendored packages) are not accepted: the Dockerfiles take the
Debian updates and drop pip, npm and corepack from the runtime images.

| ID              | Package / image                                                                                                                                          | Severity | Reason                                                                                                                                                                                    | Owner       | Expires    |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- | ---------- |
| CVE-2025-69720  | libncursesw6, libtinfo6, ncurses-base, ncurses-bin (python services and web (Debian 13))                                                                 | HIGH     | affected: no Debian fix yet: terminal library of the base image, unused by a non-interactive server                                                                                       | @Positron00 | 2027-01-10 |
| CVE-2026-119373 | libsqlite3-0 (python services and web (Debian 13))                                                                                                       | HIGH     | affected: no Debian 13 fix yet: the Python services never import sqlite3, and the web never uses `node:sqlite` (Node bundles its own SQLite), and neither uses FTS5 or session changesets | @Positron00 | 2027-01-10 |
| CVE-2026-119374 | libsqlite3-0 (python services and web (Debian 13))                                                                                                       | HIGH     | affected: no Debian 13 fix yet: the Python services never import sqlite3, and the web never uses `node:sqlite` (Node bundles its own SQLite), and neither uses FTS5 or session changesets | @Positron00 | 2027-01-10 |
| CVE-2026-119379 | libsqlite3-0 (python services and web (Debian 13))                                                                                                       | HIGH     | affected: no Debian 13 fix yet: the Python services never import sqlite3, and the web never uses `node:sqlite` (Node bundles its own SQLite), and neither uses FTS5 or session changesets | @Positron00 | 2027-01-10 |
| CVE-2026-16742  | libsystemd0, libudev1 (python services and web (Debian 13))                                                                                              | HIGH     | affected, fix_deferred: no Debian fix yet: systemd-homed is not present or run in the container, only the client libraries are installed                                                  | @Positron00 | 2027-01-10 |
| CVE-2026-54369  | libacl1 (python services and web (Debian 13))                                                                                                            | HIGH     | affected, fix_deferred: no Debian fix yet: libacl is not used by the services, which run non-root on a read-only root filesystem                                                          | @Positron00 | 2027-01-10 |
| CVE-2026-76642  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services and web (Debian 13))                   | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                                          | @Positron00 | 2027-01-10 |
| CVE-2026-78408  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services and web (Debian 13))                   | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                                          | @Positron00 | 2027-01-10 |
| CVE-2026-78409  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux (python services and web (Debian 13))                   | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                                          | @Positron00 | 2027-01-10 |
| CVE-2026-78410  | bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login, mount, util-linux, util-linux-extra (python services and web (Debian 13)) | HIGH     | affected: no Debian fix yet: mount and nsenter tools need privileges the pods drop (non-root, no capabilities) and are never run                                                          | @Positron00 | 2027-01-10 |
| CVE-2026-9538   | perl-base (python services and web (Debian 13))                                                                                                          | HIGH     | fix_deferred: Debian fix deferred: perl Archive::Tar is never run in the containers                                                                                                       | @Positron00 | 2027-01-10 |

## pip-audit (Python)

| ID  | Package | Reason | Owner | Expires |
| --- | ------- | ------ | ----- | ------- |

None accepted.

## pnpm audit (JavaScript)

| ID  | Package | Reason | Owner | Expires |
| --- | ------- | ------ | ----- | ------- |

None accepted.
