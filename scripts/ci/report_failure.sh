#!/usr/bin/env bash
# Open or update the nightly failure issue for a job (platform LLD §11.2; C38).
#
# Usage: scripts/ci/report_failure.sh <job> [log-file]     (default log: ci-logs/<job>.log)
#
# Needs `gh` authenticated with a token that may write issues (nightly.yml passes GH_TOKEN with
# job-level `issues: write`). It:
#   1. finds an open issue labelled nightly-failure titled "Nightly failure: <job>";
#   2. builds a body with the run URL, the commit and the last 150 lines of the log, passed through
#      lib.sh's scrub (JWTs, PEM blocks, Bearer tokens and password-like values masked; CI only
#      ever has throwaway values, but the repository is public);
#   3. comments on that issue, or creates it (creating the label on first use).
# A green nightly never closes the issue: a person does, once the cause is fixed.
# DRY_RUN=1 prints the title and body instead of calling gh (used for the local check).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -ge 1 && -n "$1" ]] || die "usage: scripts/ci/report_failure.sh <job> [log-file]" # not ${1:?} (bash 3.2 trap status)
job="$1"
logfile="${2:-ci-logs/$job.log}"
title="Nightly failure: $job"
label="nightly-failure"
server="${GITHUB_SERVER_URL:-https://github.com}"
repo="${GITHUB_REPOSITORY:-}"
run_url="local run (no GITHUB_RUN_ID)"
[[ -n "${GITHUB_RUN_ID:-}" && -n "$repo" ]] && run_url="$server/$repo/actions/runs/$GITHUB_RUN_ID"
sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"

body="$(mktemp)"
on_exit "rm -f '$body'"
{
  echo "The nightly job \`$job\` failed."
  echo
  echo "- Run: $run_url"
  echo "- Commit: \`$sha\`"
  echo "- Date (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "<details><summary>Last 150 lines of the log (scrubbed)</summary>"
  echo
  echo '```text'
  if [[ -f "$logfile" ]]; then
    tail -n 150 "$logfile" | scrub
  else
    echo "(no log file at $logfile)"
  fi
  echo '```'
  echo
  echo "</details>"
  echo
  echo "Close this issue once the cause is fixed; a later green run does not close it (testing.md: a flake is a bug)."
} >"$body"

if [[ "${DRY_RUN:-0}" == 1 ]]; then
  echo "title: $title"
  cat "$body"
  exit 0
fi

need gh
repo_args=()
[[ -n "$repo" ]] && repo_args=(--repo "$repo")

number="$(gh issue list ${repo_args[@]+"${repo_args[@]}"} --label "$label" --state open \
  --search "in:title \"$title\"" --json number,title \
  --jq "map(select(.title == \"$title\")) | .[0].number // empty")"

if [[ -n "$number" ]]; then
  gh issue comment ${repo_args[@]+"${repo_args[@]}"} "$number" --body-file "$body"
  log "commented on issue #$number ($title)"
else
  gh label create ${repo_args[@]+"${repo_args[@]}"} "$label" --color B60205 \
    --description "A nightly CI job failed" >/dev/null 2>&1 || true
  gh issue create ${repo_args[@]+"${repo_args[@]}"} --title "$title" --label "$label" --body-file "$body"
  log "opened an issue: $title"
fi
