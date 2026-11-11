#!/usr/bin/env bash
# CI job `contract` (platform LLD §3.15, §12; PAY-NFR-12; C21, C28).
#
# Usage: scripts/ci/contract.sh [base-ref]     (default base: origin/main)
#
#   1. Drift: export every service's OpenAPI, regenerate the TypeScript client, then
#      `git diff --exit-code` on the committed specs and generated types.
#   2. Breaking changes: oasdiff breaking <base spec> <current spec> --fail-on ERR per service,
#      skipped for a service whose spec isn't on the base ref yet (or when the ref doesn't exist).
#   3. Event schemas: the schemas are valid JSON Schema, every example validates against its schema,
#      and every example's data.company_id is the dev COMPANY_ID (C28).
# CI checks out with fetch-depth 0, so origin/main is present. Needs uv, pnpm and Docker (oasdiff).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need uv pnpm git docker

base="${1:-${CONTRACT_BASE_REF:-origin/main}}"
DEV_COMPANY_ID="0193a000-0000-7000-8000-000000000001"

group "pnpm install"
pnpm install --frozen-lockfile

group "OpenAPI export and client regeneration"
uv sync --locked --all-packages
uv run python scripts/export_openapi.py --all
pnpm --filter @payrun/api-client generate

group "contract drift (git diff --exit-code)"
if ! git diff --exit-code -- services/*/openapi.json packages/api-client; then
  die "the committed OpenAPI specs or generated client are stale: run scripts/export_openapi.py --all and pnpm --filter @payrun/api-client generate, then commit"
fi
untracked="$(git status --porcelain --untracked-files=all -- services/*/openapi.json packages/api-client/src/generated)"
[[ -z "$untracked" ]] || die "uncommitted generated contract files: $untracked"

group "breaking changes (oasdiff against $base)"
if ! git rev-parse --verify --quiet "$base^{commit}" >/dev/null; then
  log "base ref $base not found: skipping oasdiff (fetch it with: git fetch origin main)"
else
  tmp="$(mktemp -d)"
  on_exit "rm -rf '$tmp'"
  for spec in services/*/openapi.json; do
    svc="$(basename "$(dirname "$spec")")"
    if ! git cat-file -e "$base:$spec" 2>/dev/null; then
      log "$svc: no spec on $base yet, oasdiff skipped"
      continue
    fi
    git show "$base:$spec" >"$tmp/$svc.base.json"
    cp "$spec" "$tmp/$svc.head.json"
    docker run --rm -v "$tmp:/specs:ro" "$OASDIFF_IMAGE" breaking \
      "/specs/$svc.base.json" "/specs/$svc.head.json" --fail-on ERR
    log "$svc: no breaking change against $base"
  done
fi

group "event schemas (check-jsonschema)"
uv run check-jsonschema --check-metaschema contracts/events/*.json
shopt -s nullglob
for schema in contracts/events/*.json; do
  name="$(basename "$schema" .json)"
  examples=(contracts/events/examples/"$name".*.json)
  ((${#examples[@]} > 0)) || die "no examples for $schema in contracts/events/examples/"
  uv run check-jsonschema --schemafile "$schema" "${examples[@]}"
done
# Every example is claimed by exactly one schema (no orphan examples).
for ex in contracts/events/examples/*.json; do
  stem="$(basename "$ex")"
  stem="${stem%.*.json}"
  [[ -f "contracts/events/$stem.json" ]] || die "example $ex has no schema contracts/events/$stem.json"
done

group "event examples use the dev COMPANY_ID (C28)"
uv run python - "$DEV_COMPANY_ID" contracts/events/examples/*.json <<'PY'
import json
import sys

want, *files = sys.argv[1:]
bad = []
for f in files:
    doc = json.load(open(f))
    data = doc.get("data")
    if isinstance(data, str):  # an envelope carries data as a JSON string on the stream
        data = json.loads(data)
    if data is None and "envelope" in doc:  # a DLQ entry wraps the original envelope
        env = doc["envelope"]
        env = json.loads(env) if isinstance(env, str) else env
        data = env.get("data")
        data = json.loads(data) if isinstance(data, str) else data
    if not isinstance(data, dict) or data.get("company_id") != want:
        bad.append(f)
if bad:
    sys.exit("data.company_id is not the dev COMPANY_ID in: " + ", ".join(bad))
print(f"{len(files)} examples use COMPANY_ID {want}")
PY
endgroup
