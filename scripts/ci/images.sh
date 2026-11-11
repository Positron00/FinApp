#!/usr/bin/env bash
# CI job `images` (platform LLD §3.15, §10, §12; PAY-NFR-12, PAY-NFR-13; C38).
#
# Usage: scripts/ci/images.sh [--policy-only]
#        IMAGES_PLATFORM=linux/arm64 scripts/ci/images.sh    (local, native; CI uses linux/amd64)
#
#   1. .trivyignore policy (C38): every non-comment line is
#        <CVE-or-GHSA-id> # reason: <text>; owner: <github handle>; exp:YYYY-MM-DD
#      with an expiry that has not passed, and the ID listed in SECURITY_DEPENDENCIES.md.
#      (Trivy itself also honours the exp: field, so an expired entry stops suppressing.)
#   2. docker buildx bake for linux/amd64 (PAY-NFR-13), loaded as payrun/<img>:ci.
#   3. trivy image --severity HIGH,CRITICAL --exit-code 1 --ignorefile .trivyignore, per image.
# Trivy runs in its pinned image against the local Docker daemon; its DB is cached in
# TRIVY_CACHE_DIR (default ~/.cache/trivy; CI caches that path).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker git python3

group ".trivyignore policy (C38)"
python3 - .trivyignore SECURITY_DEPENDENCIES.md <<'PY'
import datetime as dt
import re
import sys

ignore_file, register = sys.argv[1:]
LINE = re.compile(
    r"^(?P<id>(CVE-\d{4}-\d{4,}|GHSA(-[23456789cfghjmpqrvwx]{4}){3}))\s+#\s*"
    r"reason:\s*(?P<reason>[^;]*\S)\s*;\s*owner:\s*(?P<owner>@?[A-Za-z0-9][A-Za-z0-9-]*)\s*;\s*"
    r"exp:(?P<exp>\d{4}-\d{2}-\d{2})\s*$"
)
listed = open(register, encoding="utf-8").read()
today = dt.datetime.now(dt.UTC).date()
errors, count = [], 0
for n, raw in enumerate(open(ignore_file, encoding="utf-8"), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    count += 1
    m = LINE.match(line)
    if not m:
        errors.append(f"line {n}: expected '<ID> # reason: <text>; owner: <handle>; exp:YYYY-MM-DD'")
        continue
    try:
        exp = dt.date.fromisoformat(m["exp"])
    except ValueError:
        errors.append(f"line {n}: {m['id']}: invalid exp date {m['exp']}")
        continue
    if exp < today:
        errors.append(f"line {n}: {m['id']} expired on {exp}: fix it or renew the acceptance")
    if m["id"] not in listed:
        errors.append(f"line {n}: {m['id']} is not recorded in {register}")
if errors:
    sys.exit("\n".join([".trivyignore policy violations:", *errors]))
print(f".trivyignore policy: {count} accepted finding(s), all with reason, owner and a future expiry")
PY
[[ "${1:-}" == --policy-only ]] && exit 0

platform="${IMAGES_PLATFORM:-linux/amd64}"
group "docker buildx bake ($platform)"
bake_images ci "$platform"

cache="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy}"
mkdir -p "$cache"
group "trivy DB"
retry 3 docker run --rm -v "$cache:/root/.cache/trivy" "$TRIVY_IMAGE" image --download-db-only --no-progress

status=0
for img in "${IMAGES[@]}"; do
  group "trivy payrun/$img:ci (HIGH, CRITICAL)"
  if ! docker run --rm -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$cache:/root/.cache/trivy" -v "$CI_ROOT/.trivyignore:/work/.trivyignore:ro" \
    "$TRIVY_IMAGE" image --skip-db-update --no-progress --scanners vuln \
    --severity HIGH,CRITICAL --exit-code 1 --ignorefile /work/.trivyignore \
    "payrun/$img:ci"; then
    status=1
    log "payrun/$img:ci has HIGH or CRITICAL findings"
  fi
done
endgroup
exit "$status"
