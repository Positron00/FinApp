#!/usr/bin/env bash
# Copy the shared deploy assets into the Kustomize tree, byte for byte (platform LLD §3.15; R32, C36).
#
# Usage: scripts/sync-k8s-assets.sh [--check]
#
# Kustomize can't load files outside a kustomization's root, and the overlays are built with no
# load-restrictor flag (C36), so the copies are committed. Edit only the sources:
#
#   deploy/compose/init/                      -> deploy/k8s/base/bootstrap/init/
#   deploy/compose/redis/{redis.conf,entrypoint.sh}
#                                             -> deploy/k8s/components/in-cluster-data/redis/
#   deploy/observability/collector.yaml       -> deploy/k8s/base/otel-collector/collector.yaml
#   deploy/observability/collector.prod.yaml  -> deploy/k8s/overlays/prod/collector.prod.yaml
#
# Copy files whose source is gone are deleted. --check copies nothing and exits 1 listing every
# copy that differs, is missing or has no source. scripts/ci/manifests.sh runs the sync and then
# `git diff --exit-code` on the copy paths, so a stale committed copy fails CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

check=0
case "${1:-}" in
  --check) check=1 ;;
  "") ;;
  -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
  *) echo "sync-k8s-assets.sh: unknown argument: $1" >&2; exit 2 ;;
esac

# "source-dir|copy-dir|files" (files: space-separated relative paths, or * for the whole tree).
MAPPINGS=(
  "deploy/compose/init|deploy/k8s/base/bootstrap/init|*"
  "deploy/compose/redis|deploy/k8s/components/in-cluster-data/redis|redis.conf entrypoint.sh"
  "deploy/observability|deploy/k8s/base/otel-collector|collector.yaml"
  "deploy/observability|deploy/k8s/overlays/prod|collector.prod.yaml"
)

problems=()

list_files() { # list_files <dir>: relative file paths under dir, sorted
  (cd "$1" && find . -type f ! -name '.DS_Store' | sed 's|^\./||' | LC_ALL=C sort)
}

for m in "${MAPPINGS[@]}"; do
  IFS='|' read -r src dst files <<<"$m"
  [[ -d "$src" ]] || { echo "sync-k8s-assets.sh: missing source directory $src" >&2; exit 1; }
  if [[ "$files" == "*" ]]; then
    wanted="$(list_files "$src")"
    # Whole-tree copy: anything in the copy without a source is stale.
    if [[ -d "$dst" ]]; then
      while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        if [[ ! -f "$src/$f" ]]; then
          problems+=("$dst/$f (no source)")
          [[ "$check" == 1 ]] || rm -f "$dst/$f"
        fi
      done < <(list_files "$dst")
      [[ "$check" == 1 ]] || find "$dst" -type d -empty -delete
    fi
  else
    wanted="$(printf '%s\n' "$files" | tr ' ' '\n')"
  fi
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if [[ ! -f "$src/$f" ]]; then
      echo "sync-k8s-assets.sh: missing source file $src/$f" >&2
      exit 1
    fi
    if [[ ! -f "$dst/$f" ]]; then
      problems+=("$dst/$f (missing)")
    elif ! cmp -s "$src/$f" "$dst/$f"; then
      problems+=("$dst/$f (differs from $src/$f)")
    else
      continue
    fi
    if [[ "$check" == 0 ]]; then
      mkdir -p "$(dirname "$dst/$f")"
      cp -p "$src/$f" "$dst/$f"
    fi
  done <<<"$wanted"
done

if [[ "$check" == 1 ]]; then
  if ((${#problems[@]} > 0)); then
    echo "sync-k8s-assets.sh: copies are out of date (run scripts/sync-k8s-assets.sh):" >&2
    printf '  %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "sync-k8s-assets.sh: all copies match their sources"
  exit 0
fi

if ((${#problems[@]} > 0)); then
  echo "sync-k8s-assets.sh: synced:"
  printf '  %s\n' "${problems[@]}"
else
  echo "sync-k8s-assets.sh: nothing to do"
fi
