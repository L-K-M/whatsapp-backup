#!/usr/bin/env bash
# update.sh - sync the latest app code, rebuild the image, and restart.
#
# The Dockerfile clones upstream wacli from the WACLI_REF *branch* at build
# time, so a plain `docker compose build` would reuse the cached layer and
# keep the old wacli build. `--no-cache` is therefore required on every
# update.
#
# Usage: sudo ./update.sh
set -euo pipefail

cd "$(dirname "$0")"

# When started via sudo, run git as the invoking user so the working tree
# keeps its ownership and git's dubious-ownership checks stay satisfied.
run_git() {
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    sudo -u "$SUDO_USER" -H git "$@"
    return
  fi
  git "$@"
}

if [ ! -f .env ]; then
  echo "error: .env not found - run 'cp .env.example .env' and adjust it first" >&2
  exit 1
fi

run_git fetch origin main

# Local modifications would abort the pull. This is a deployment checkout
# that tracks upstream, so save the diff to a patch and reset to origin/main.
# Untracked files (.env, data/) are not touched.
if ! run_git diff --quiet HEAD; then
  backup="local-changes-$(date +%Y%m%d-%H%M%S).patch"
  run_git diff HEAD >"$backup"
  echo "Saved local modifications to $backup"
fi
run_git reset --hard origin/main

docker compose down
docker compose build --no-cache
docker compose up -d

echo "Updated and restarted. UI: http://<your-host-ip>:${WEB_PORT:-64009}"
