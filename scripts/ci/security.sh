#!/usr/bin/env bash
# CI job `security` (platform LLD §3.15, §12; PAY-NFR-12).
#
# Usage: scripts/ci/security.sh [--no-gitleaks]
#        GITLEAKS_BASE=<sha|ref> scripts/ci/security.sh   (CI: the PR base or the push's `before`)
#
#   1. pip-audit --strict on `uv export` (the locked runtime dependencies of every workspace member),
#      ignoring only the IDs accepted in SECURITY_DEPENDENCIES.md (expired acceptances fail).
#   2. pnpm audit --audit-level high (accepted GHSAs live in package.json pnpm.auditConfig).
#   3. gitleaks over the commits in <base>..HEAD (default base origin/main), honouring
#      .gitleaksignore. With no base (a first push, an all-zero `before`) or an empty range it scans
#      the whole history, so a local run on main still proves something.
# Fresh advisory databases are fetched on every run (nightly-audit.sh runs this too).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need uv pnpm git docker python3

# IDs from a SECURITY_DEPENDENCIES.md section table: | ID | ... | Expires |; fails on an expired row.
accepted_ids() {
  python3 - "$1" <<'PY'
import datetime as dt
import re
import sys

section = sys.argv[1]
text = open("SECURITY_DEPENDENCIES.md", encoding="utf-8").read()
m = re.search(rf"^## {re.escape(section)}.*?(?=^## |\Z)", text, flags=re.M | re.S)
if not m:
    sys.exit(f"SECURITY_DEPENDENCIES.md has no '## {section}' section")
today = dt.datetime.now(dt.UTC).date()
for row in re.findall(r"^\|(.+)\|\s*$", m.group(0), flags=re.M):
    cells = [c.strip() for c in row.split("|")]
    if not cells[0] or cells[0] == "ID" or set(cells[0]) <= {"-"}:
        continue
    exp = dt.date.fromisoformat(cells[-1])
    if exp < today:
        sys.exit(f"SECURITY_DEPENDENCIES.md: {cells[0]} expired on {exp}")
    print(cells[0])
PY
}

group "pip-audit (uv export, --strict)"
ignore_args=()
accepted="$(accepted_ids "pip-audit (Python)")"
for id in $accepted; do ignore_args+=(--ignore-vuln "$id"); done
reqs="$(mktemp)"
on_exit "rm -f '$reqs'"
uv export --color never --frozen --all-packages --no-dev --no-hashes --no-emit-workspace \
  --no-header --format requirements-txt -o "$reqs" >/dev/null
log "auditing $(grep -c '==' "$reqs") pinned Python packages ($((${#ignore_args[@]} / 2)) accepted)"
uv run pip-audit --strict --progress-spinner off --no-deps --disable-pip -r "$reqs" ${ignore_args[@]+"${ignore_args[@]}"}

group "pnpm audit --audit-level high"
pnpm audit --audit-level high

if [[ "${1:-}" == --no-gitleaks ]]; then
  endgroup
  exit 0
fi

group "gitleaks"
base="${GITLEAKS_BASE:-origin/main}"
log_opts=()
if [[ "$base" =~ ^0+$ ]] || ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null; then
  log "no usable base ($base): scanning the whole history"
elif [[ "$(git rev-list --count "$base..HEAD")" == 0 ]]; then
  log "no commits in $base..HEAD: scanning the whole history"
else
  log "scanning $(git rev-list --count "$base..HEAD") commit(s) in $base..HEAD"
  log_opts=(--log-opts="$base..HEAD")
fi
docker run --rm --user "$(id -u):$(id -g)" -v "$CI_ROOT:/repo:ro" -w /repo \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$GITLEAKS_IMAGE" git /repo ${log_opts[@]+"${log_opts[@]}"} --redact --exit-code 1 --no-banner \
  --gitleaks-ignore-path /repo/.gitleaksignore
endgroup
