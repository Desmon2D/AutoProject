#!/bin/sh
set -eu

: "${GITEA_TOKEN:?GITEA_TOKEN is required}"
: "${GITEA_USERNAME:?GITEA_USERNAME is required}"
: "${GITEA_REPOSITORY:?GITEA_REPOSITORY is required}"

credential="$(printf '%s:%s' "$GITEA_USERNAME" "$GITEA_TOKEN" | base64 | tr -d '\n')"
remote="http://gitea:3000/${GITEA_USERNAME}/${GITEA_REPOSITORY}.git"
repository_dir="$(mktemp -d)"
branch="automation-connectivity"
trap 'rm -rf "$repository_dir"' EXIT

git -c "http.extraHeader=Authorization: Basic $credential" clone "$remote" "$repository_dir" >/dev/null
git -C "$repository_dir" config user.name "Automation Connectivity Check"
git -C "$repository_dir" config user.email "automation@local.invalid"
if git -C "$repository_dir" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
  git -C "$repository_dir" checkout -B "$branch" "origin/$branch" >/dev/null
else
  git -C "$repository_dir" checkout -b "$branch" >/dev/null
fi
git -C "$repository_dir" commit --allow-empty \
  -m "Verify agent trigger $(date -u +%Y-%m-%dT%H:%M:%SZ)-$$" >/dev/null
git -C "$repository_dir" -c "http.extraHeader=Authorization: Basic $credential" \
  push origin "HEAD:refs/heads/$branch" >/dev/null
echo "Git read/write source check passed: ${GITEA_USERNAME}/${GITEA_REPOSITORY}"
