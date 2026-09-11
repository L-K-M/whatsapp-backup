#!/usr/bin/env bash
# Update this whatsapp-backup deployment:
# sync code from origin, rebuild the image, restart the container.
# Untracked files (.env, data/) are never touched.

set -euo pipefail

cd "$(dirname "$0")"

# Docker on TrueNAS is root-only; use sudo unless already root.
DOCKER=(docker)
if [ "$(id -u)" -ne 0 ]; then
  DOCKER=(sudo docker)
fi

git fetch origin main

# Deployment checkout: force-sync to origin/main. Local edits would block
# a plain pull, so back them up to a patch file before resetting.
if ! git diff --quiet HEAD; then
  backup="local-changes-$(date +%Y%m%d-%H%M%S).patch"
  git diff HEAD >"$backup"
  echo "Saved local modifications to $backup"
fi
git reset --hard origin/main

# wacli is cloned from git during build, so its cached layer must be
# invalidated manually; --no-cache-filter keeps the other stages cached.
"${DOCKER[@]}" compose build --no-cache-filter wacli-builder

"${DOCKER[@]}" compose up -d --remove-orphans

"${DOCKER[@]}" image prune -f

"${DOCKER[@]}" compose ps
