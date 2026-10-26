#!/bin/sh
# check-image-notices.sh: verify the notices inside a built, locally loaded
# image, as CI and release workflows must before pushing it:
#   1. each named file in DOC-DIR exists in the image and is not empty;
#   2. the image's org.opencontainers.image.licenses label matches the
#      licenses its notices name (oci-licenses.py check). Read for that:
#      OS-Packages.txt and Third-Party-Notices.txt when the image has them,
#      and every other named file with entries in the generators' format (a rule line of "=", a name
#      line, "License: <SPDX expression>"). Other named files (for example
#      Rust-Licenses.html or a hand-written notices file) get a note: their
#      licenses must reach the label through --add.
#
#   sh scripts/check-image-notices.sh IMAGE:TAG /usr/share/doc/IMAGE \
#       Third-Party-Notices.txt OS-Packages.txt LICENSE \
#       -- --own Unlicense --package tini=MIT
#
# Arguments after "--" go to oci-licenses.py check (--own is required).
# Needs docker and python3; works for images without a shell.
set -eu

[ $# -ge 3 ] || { echo "usage: $0 IMAGE DOC-DIR FILE... [-- OCI-LICENSES-ARGS...]" >&2; exit 2; }
image=$1 dir=${2%/}
shift 2
tmp=$(mktemp -d)
cid=""
trap 'rm -rf "$tmp"; [ -z "$cid" ] || docker rm "$cid" >/dev/null' EXIT INT TERM

cid=$(docker create "$image")
mkdir "$tmp/doc"
docker cp "$cid:$dir/." "$tmp/doc/"
status=0
: > "$tmp/notices"
while [ $# -gt 0 ] && [ "$1" != -- ]; do
  if [ -s "$tmp/doc/$1" ]; then
    echo "ok: $dir/$1"
    case "$1" in
      Third-Party-Notices.txt|OS-Packages.txt|LICENSE|LICENSE.*|COPYING|COPYING.*) ;;
      *)
        if awk 'p2 ~ /^====================/ && p1 != "" && /^License: / { found = 1; exit }
                { p2 = p1; p1 = $0 } END { exit !found }' "$tmp/doc/$1"; then
          echo "$tmp/doc/$1" >> "$tmp/notices"
        else
          echo "note: $dir/$1 has no generator-format entries; name its licenses in the label with --add"
        fi ;;
    esac
  else
    echo "error: $dir/$1 is missing or empty in $image" >&2
    status=1
  fi
  shift
done
[ $# -eq 0 ] || shift

# The labels as JSON ("null" when the image has none), so inspect errors fail the run.
labels=$(docker image inspect -f '{{json .Config.Labels}}' "$image")
label=$(printf '%s' "$labels" | python3 -c 'import json, sys
print((json.load(sys.stdin) or {}).get("org.opencontainers.image.licenses", ""))')
set -- "$@" --label "$label"
if [ -f "$tmp/doc/OS-Packages.txt" ]; then set -- "$@" --os-packages "$tmp/doc/OS-Packages.txt"; fi
if [ -f "$tmp/doc/Third-Party-Notices.txt" ]; then set -- "$@" "$tmp/doc/Third-Party-Notices.txt"; fi
while read -r file; do set -- "$@" "$file"; done < "$tmp/notices"
python3 "$(dirname "$0")/oci-licenses.py" check "$@" || status=1
exit "$status"
