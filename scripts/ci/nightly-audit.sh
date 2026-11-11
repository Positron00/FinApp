#!/usr/bin/env bash
# Nightly job `audit` (platform LLD §3.15, §11.2; PAY-NFR-12).
#
# Usage: scripts/ci/nightly-audit.sh
#
# The dependency audits against fresh advisory databases, then a fresh image build and Trivy scan:
#   - security.sh --no-gitleaks (pip-audit and pnpm audit query their advisory services live);
#   - images.sh with an empty Trivy cache (TRIVY_CACHE_DIR in a temp dir), so the vulnerability DB
#     is downloaded new and no cached result can hide a new CVE. The .trivyignore policy runs too,
#     so an acceptance that expired overnight fails here first.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

status=0
group "dependency audits (fresh advisories)"
scripts/ci/security.sh --no-gitleaks || status=1

group "images + Trivy (fresh DB)"
fresh="$(mktemp -d)"
on_exit "rm -rf '$fresh'"
TRIVY_CACHE_DIR="$fresh" scripts/ci/images.sh || status=1
endgroup
exit "$status"
