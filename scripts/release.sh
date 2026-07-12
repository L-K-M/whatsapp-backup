#!/usr/bin/env bash
# Cuts a release: pre-flight checks (clean tree, new tag), tags HEAD as "v<version>",
# and with --push pushes branch + tag — which triggers .github/workflows/release.yml
# to build the Docker image and push it to GHCR (ghcr.io/l-k-m/whatsapp-backup, tagged
# X.Y.Z, X.Y, and latest; :latest skipped for pre-releases like v1.2.3-rc.1). Nothing
# committed declares a version — the Dockerfile builds upstream wacli from the
# WACLI_REF *branch* (main) and CI's metadata-action derives every image tag from the
# git tag — so the tag is the only source of truth: nothing to bump or commit, and the
# version argument is required.
#
#   scripts/release.sh 1.2.3          # check tree is clean + tag is new, tag v1.2.3
#   scripts/release.sh 1.2.3 --push   # …also push HEAD + tag (CI then builds + publishes)
#
# Usage: scripts/release.sh X.Y.Z[-pre] [--push]
# Shared engine: https://github.com/L-K-M/release-tool (this stub only sets config).
set -euo pipefail

export RELEASE_APP_NAME="whatsapp-backup"
export RELEASE_KIND="tag-only"
export RELEASE_CI_NOTE="CI (release.yml) will now build the Docker image and publish ghcr.io/l-k-m/whatsapp-backup:<version> (stable releases also get :major.minor and :latest)."
export RELEASE_INVOKED_AS="scripts/release.sh"

BIN="${LKM_RELEASE_BIN:-lkm-release}"
command -v "$BIN" >/dev/null 2>&1 || {
  echo "error: lkm-release not found — clone https://github.com/L-K-M/release-tool and run ./install.sh" >&2
  exit 1
}
exec "$BIN" "$@"
