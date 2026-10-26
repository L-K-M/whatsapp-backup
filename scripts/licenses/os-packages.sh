#!/bin/sh
# os-packages.sh: list the OS packages of a Debian/Ubuntu (dpkg) or Alpine
# (apk) image with their exact source package versions and source locations,
# check that each one's license text is in the image, and write it all to
# one notices file. Run it inside the image, after the final stage's last
# package installation, so it sees exactly what ships:
#
#   RUN --mount=type=bind,source=scripts/os-packages.sh,target=/tmp/os-packages.sh \
#       sh /tmp/os-packages.sh --image NAME /usr/share/doc/NAME/OS-Packages.txt
#
# Debian keeps every package's license text in /usr/share/doc/<pkg>/copyright
# (slim images too); the script only checks it is there. Alpine packages ship
# no license files, so for Alpine the canonical SPDX texts of every license
# the packages declare are appended (from --spdx-dir, or downloaded from the
# pinned SPDX license-list-data tag with --spdx-tag). Licenses whose terms
# require the package's own copyright or NOTICE lines (MIT, BSD, ISC,
# Apache-2.0 and the like, anything not in complete_license below) also get
# the license files of the package's upstream source: the APKBUILD at the
# package's aports commit names the archives, which are downloaded from the
# distfiles mirror (or upstream), checked against the APKBUILD's sha512sums,
# and their top-level LICENSE*/COPYING*/NOTICE* files appended. Packages that
# install their own texts under /usr/share/licenses are pointed there instead.
#
# Fails (exit 1) when a package has no license text and no override, so a
# new package cannot ship silently. Overrides file, one package per line,
# a reason after "#" is required:
#   <package> <path to its license text inside the image>  # reason
#   <package> -                                             # reason no text is needed
#   <package> canonical                                     # reason the canonical SPDX
#                                         texts suffice (Alpine packages whose source
#                                         has no license file and no notice to keep)
#
# Options:
#   --image NAME      image name for the header
#   --root DIR        read the image filesystem below DIR (default /)
#   --base-db FILE    package database of the base image (its status or
#                     installed file): packages not in it get "Added: yes",
#                     and those the build installed by name (apk world, not
#                     apt Auto-Installed) also "Requested: yes". Needed for
#                     oci-licenses.py, which refuses files written without it
#   --overrides FILE  overrides as above
#   --spdx-dir DIR    canonical texts as DIR/<SPDX-ID>.txt (Alpine)
#   --spdx-tag TAG    download missing texts from spdx/license-list-data TAG
#                     (default v3.29.0 when neither option is given on Alpine)
#   --cache DIR       keep the license files read from Alpine sources in DIR
#                     (a BuildKit cache mount), so rebuilds and other
#                     platforms skip the downloads
#
# POSIX sh and awk only (busybox works). Downloads need wget or curl; Alpine
# images need network access to aports and the distfiles mirror.
set -eu

ROOT=/ BASE_DB="" OVERRIDES="" SPDX_DIR="" SPDX_TAG="" IMAGE="" OUT="" CACHE=""
DEFAULT_SPDX_TAG=v3.29.0
SPDX_URL=https://raw.githubusercontent.com/spdx/license-list-data
# APKBUILDs at a commit: Alpine's GitLab, then its GitHub mirror.
APORTS_RAW="https://gitlab.alpinelinux.org/alpine/aports/-/raw https://raw.githubusercontent.com/alpinelinux/aports"
DISTFILES=https://distfiles.alpinelinux.org/distfiles
RULE=$(printf '%72s' '' | tr ' ' '=')

die() { echo "os-packages.sh: error: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE=$2; shift 2 ;;
    --root) ROOT=$2; shift 2 ;;
    --base-db) BASE_DB=$2; shift 2 ;;
    --overrides) OVERRIDES=$2; shift 2 ;;
    --spdx-dir) SPDX_DIR=$2; shift 2 ;;
    --spdx-tag) SPDX_TAG=$2; shift 2 ;;
    --cache) CACHE=$2; shift 2 ;;
    -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$OUT" ] || die "one output file only"; OUT=$1; shift ;;
  esac
done
[ -n "$OUT" ] || die "usage: os-packages.sh [options] OUTPUT-FILE"
ROOT=${ROOT%/}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

if [ -f "$ROOT/var/lib/dpkg/status" ]; then
  KIND=dpkg DB=/var/lib/dpkg/status
elif [ -f "$ROOT/lib/apk/db/installed" ]; then
  KIND=apk DB=/lib/apk/db/installed
else
  die "no dpkg or apk package database below ${ROOT:-/}"
fi
if [ "$KIND" = apk ] && [ -z "$SPDX_DIR" ] && [ -z "$SPDX_TAG" ]; then
  SPDX_TAG=$DEFAULT_SPDX_TAG
fi

# One line per installed package: name|version|arch|license|source|source version|commit
parse_db() {
  if [ "$KIND" = dpkg ]; then
    awk '
      function flush() {
        if (pkg != "" && status ~ / installed$/) {
          sname = src; sver = ver
          if (sname == "") sname = pkg
          if (sname ~ / \(/) { sver = sname; sub(/^[^(]*\(/, "", sver); sub(/\).*$/, "", sver); sub(/ .*$/, "", sname) }
          print pkg "|" ver "|" arch "||" sname "|" sver "|"
        }
        pkg = ver = arch = src = status = ""
      }
      /^$/ { flush(); next }
      /^Package: / { pkg = substr($0, 10) }
      /^Status: / { status = substr($0, 9) }
      /^Version: / { ver = substr($0, 10) }
      /^Architecture: / { arch = substr($0, 15) }
      /^Source: / { src = substr($0, 9) }
      END { flush() }' "$1"
  else
    awk '
      function flush() {
        # Names starting with "." are apk add --virtual groups: metadata, no files.
        if (pkg != "" && pkg !~ /^\./) print pkg "|" ver "|" arch "|" lic "|" (origin == "" ? pkg : origin) "|" ver "|" commit
        pkg = ver = arch = lic = origin = commit = ""
      }
      /^$/ { flush(); next }
      /^P:/ { pkg = substr($0, 3) }
      /^V:/ { ver = substr($0, 3) }
      /^A:/ { arch = substr($0, 3) }
      /^L:/ { lic = substr($0, 3) }
      /^o:/ { origin = substr($0, 3) }
      /^c:/ { commit = substr($0, 3) }
      END { flush() }' "$1"
  fi | LC_ALL=C sort -u -t"|" -k1,1 -k2
}

parse_db "$ROOT$DB" > "$TMP/packages"
[ -s "$TMP/packages" ] || die "no installed packages in $ROOT$DB"
: > "$TMP/base"
if [ -n "$BASE_DB" ]; then
  [ -f "$BASE_DB" ] || die "--base-db $BASE_DB not found"
  parse_db "$BASE_DB" | cut -d'|' -f1 > "$TMP/base"
else
  echo "os-packages.sh: warning: no --base-db, so added and requested packages are not marked; oci-licenses.py will refuse $OUT" >&2
fi
# Packages the image build asked for by name, as opposed to dependencies.
# Without the record of that, every added package counts as requested, so
# none drops out of the label check unnoticed.
: > "$TMP/requested"
if [ -n "$BASE_DB" ] && [ "$KIND" = apk ] && [ -f "$ROOT/etc/apk/world" ]; then
  sed 's/[<>=~@].*//' "$ROOT/etc/apk/world" > "$TMP/world"
  # apk add --virtual .name pkgs: world holds .name; its dependencies are the request.
  awk -v world="$TMP/world" '
    BEGIN { while ((getline w < world) > 0) want[w] = 1 }
    /^P:/ { p = substr($0, 3) }
    /^D:/ && (p in want) && p ~ /^\./ {
      n = split(substr($0, 3), deps, " ")
      for (i = 1; i <= n; i++) { d = deps[i]; sub(/[<>=~].*/, "", d); if (d !~ /:/ && d !~ /^!/) print d }
    }' "$ROOT$DB" | cat "$TMP/world" - > "$TMP/requested"
elif [ -n "$BASE_DB" ] && [ -f "$ROOT/var/lib/apt/extended_states" ]; then
  awk '/^Package: /{p=substr($0,10)} /^Auto-Installed: 1/{print p}' \
    "$ROOT/var/lib/apt/extended_states" > "$TMP/auto"
  cut -d'|' -f1 "$TMP/packages" | grep -v -x -F -f "$TMP/auto" > "$TMP/requested" || true
elif [ -n "$BASE_DB" ]; then
  echo "os-packages.sh: warning: no apk world or apt extended_states; marking every added package as requested" >&2
  cut -d'|' -f1 "$TMP/packages" > "$TMP/requested"
fi
: > "$TMP/overrides"
if [ -n "$OVERRIDES" ]; then
  [ -f "$OVERRIDES" ] || die "--overrides $OVERRIDES not found"
  awk '
    /^[ \t]*(#|$)/ { next }
    {
      reason = $0; sub(/^[^#]*#?/, "", reason); gsub(/^[ \t]+|[ \t]+$/, "", reason)
      if (index($0, "#") == 0 || reason == "" || NF < 3) { print "BAD|" NR; next }
      print $1 "|" $2
    }' "$OVERRIDES" > "$TMP/overrides"
  bad=$(grep '^BAD|' "$TMP/overrides" | cut -d'|' -f2 | tr '\n' ' ' || true)
  [ -z "$bad" ] || die "$OVERRIDES: lines $bad need '<package> <path|-|canonical> # reason'"
fi

# Pretty distribution name and, for Alpine, the release branch.
DISTRO=$(cat "$ROOT/etc/os-release" "$ROOT/usr/lib/os-release" 2>/dev/null | sed -n 's/^PRETTY_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' | head -n 1)
[ -n "$DISTRO" ] || DISTRO="unknown distribution"
DISTRO_ID=$(cat "$ROOT/etc/os-release" "$ROOT/usr/lib/os-release" 2>/dev/null | sed -n 's/^ID="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' | head -n 1)
BRANCH="" APK_ARCH=""
if [ "$KIND" = apk ]; then
  # Releases look like 3.22.2; edge snapshots carry suffixes such as _alpha.
  BRANCH=$(sed -n 's/^\([0-9]*\.[0-9]*\)\.[0-9]*$/v\1/p' "$ROOT/etc/alpine-release" 2>/dev/null || true)
  [ -n "$BRANCH" ] || BRANCH=edge
  APK_ARCH=$(cat "$ROOT/etc/apk/arch" 2>/dev/null || uname -m)
fi
# busybox tar reads gzip, bzip2 and xz itself; GNU tar would need those programs.
TAR=tar
if command -v busybox >/dev/null 2>&1; then TAR="busybox tar"; fi

fetch() { # url file
  if command -v wget >/dev/null 2>&1 && wget -q -T 60 -O "$2" "$1" 2>/dev/null; then return 0; fi
  command -v curl >/dev/null 2>&1 && curl -fsL -o "$2" "$1"
}

# Licenses whose canonical text is all the package needs: copyleft licenses
# (their source pointer is the aports commit and the distfiles mirror),
# exceptions, and licenses without a notice to reproduce in binaries.
# Everything else needs the package's own copyright or NOTICE lines.
complete_license() {
  case "$1" in
    GPL-*|LGPL-*|AGPL-*|MPL-*|EPL-*|CDDL-*|EUPL-*|*-exception*) return 0 ;;
    LicenseRef-public-domain|CC0-1.0|Unlicense|0BSD|MIT-0|WTFPL|Zlib|BSL-1.0|blessing) return 0 ;;
  esac
  return 1
}

# License files of an Alpine package's upstream source, read once per source
# package and version into source_dir: its texts file lists "label|file"
# (relative to that directory) for each one found. Prints nothing when license
# files were found, otherwise the reason. Only complete successes are kept
# (marked by "done"), so a network failure does not stick in --cache.
source_dir() { echo "${CACHE:-$TMP/src}/$1-$2"; }
source_texts() { # origin version commit
  d=$(source_dir "$1" "$2") failed=$TMP/failed-$1-$2
  [ ! -f "$d/done" ] || return 0
  if [ -f "$failed" ]; then cat "$failed"; return 0; fi
  rm -rf "$d"; mkdir -p "$d"
  why=$(source_fetch "$1" "$2" "$3" "$d" 2>/dev/null || echo "reading its source failed")
  if [ -n "$why" ]; then
    echo "$why" > "$failed"; rm -rf "$d"; echo "$why"
  else
    : > "$d/done"
  fi
}

source_fetch() { # origin version commit dir
  o=$1 v=$2 c=$3 d=$4
  : > "$d/texts"
  [ -n "$c" ] || { echo "no aports commit in the package database"; return 0; }
  for base in $APORTS_RAW; do
    for repo in main community testing; do
      if fetch "$base/$c/$repo/$o/APKBUILD" "$d/APKBUILD" && [ -s "$d/APKBUILD" ]; then break 2; fi
      rm -f "$d/APKBUILD"
    done
  done
  [ -s "$d/APKBUILD" ] || { echo "no APKBUILD for $o at aports commit $c"; return 0; }
  # Expand source= and sha512sums= as abuild does, by sourcing the APKBUILD
  # (from the commit that built the installed package) in a subshell.
  # shellcheck disable=SC1091,SC2034,SC2154  # variables set and read by the APKBUILD
  ( set +eu
    cd "$d" || exit 0
    CARCH=$APK_ARCH CTARGET_ARCH=$APK_ARCH srcdir=$d/src startdir=$d
    . ./APKBUILD >/dev/null 2>&1 </dev/null
    echo "V $pkgver-r$pkgrel"
    for s in $source; do echo "S $s"; done
    printf '%s\n' "$sha512sums" | awk 'NF == 2 && length($1) == 128 { print "H " $1 " " $2 }'
  ) > "$d/meta"
  got=$(sed -n 's/^V //p' "$d/meta")
  [ "$got" = "$v" ] || { echo "the APKBUILD at aports commit $c is for version ${got:-unknown}, not $v"; return 0; }
  : > "$d/problems"; archives=""
  sed -n 's/^S //p' "$d/meta" > "$d/sources"
  while read -r s; do
    case "$s" in
      *::*) name=${s%%::*} url=${s#*::} ;;
      *) name=${s##*/} url=$s ;;
    esac
    case "$url" in *://*) ;; *) continue ;; esac  # files kept in aports (patches, scripts)
    case "$name" in
      *.tar.gz|*.tgz) z=z ;;
      *.tar.bz2|*.tbz2|*.tbz) z=j ;;
      *.tar.xz|*.txz) z=J ;;
      *.tar) z= ;;
      *) continue ;;  # not a tar archive: single files, or zip/zstd/lzip, which busybox cannot list
    esac
    archives="$archives $name"
    want=$(awk -v f="$name" '$1 == "H" && $3 == f { print $2; exit }' "$d/meta")
    [ -n "$want" ] || { echo "$name has no sha512sum in the APKBUILD" >> "$d/problems"; continue; }
    ok=
    for u in "$DISTFILES/$BRANCH/$name" "$url"; do
      rm -f "$d/archive"
      if fetch "$u" "$d/archive" && [ "$(sha512sum "$d/archive" | cut -d' ' -f1)" = "$want" ]; then ok=yes; break; fi
    done
    [ -n "$ok" ] || { echo "$name: download failed or its sha512sum does not match" >> "$d/problems"; continue; }
    # Top-level license and notice files (and a REUSE LICENSES/ directory).
    if ! $TAR -t"${z}"f "$d/archive" > "$d/list"; then
      echo "$name: cannot list the archive" >> "$d/problems"; rm -f "$d/archive"; continue
    fi
    awk '{
        name = $0; sub(/^\.\//, "", name)
        if (name == "" || name ~ /\/$/ || name ~ /(^|\/)\.\.(\/|$)/) next
        n = split(name, part, "/"); base = tolower(part[n])
        if (base ~ /\.(c|h|cc|cpp|cxx|go|py|js|ts|json|ya?ml|toml|cfg|sh|pl|rb|rs|java|m4|am|cmake|mk|xml|png|svg|gif|jpg)$/) next
        named = base ~ /^(licen[cs]es?|copying|copyright|notices?|unlicense|patents)([-_. 0-9].*)?$/ ||
                base ~ /^[a-z0-9_.-]+[-_.](licen[cs]es?|copying|notices?)([-_. 0-9].*)?$/
        reuse = (n == 2 && tolower(part[1]) ~ /^licen[cs]es$/) || (n == 3 && tolower(part[2]) ~ /^licen[cs]es$/)
        if ((n <= 2 && named) || reuse) print $0
      }' "$d/list" | LC_ALL=C sort > "$d/members"
    if [ -s "$d/members" ]; then
      mkdir -p "$d/x/$name"
      if $TAR -x"${z}"f "$d/archive" -C "$d/x/$name" -T "$d/members"; then
        while read -r m; do
          if [ -f "$d/x/$name/$m" ] && [ -s "$d/x/$name/$m" ]; then
            echo "$name: ${m#./}|x/$name/$m" >> "$d/texts"
          fi
        done < "$d/members"
      else
        echo "$name: cannot extract its license files" >> "$d/problems"
      fi
    fi
    rm -f "$d/archive" "$d/list"
  done < "$d/sources"
  if [ -s "$d/problems" ]; then
    printf 'source of %s %s: %s\n' "$o" "$v" "$(tr '\n' ';' < "$d/problems" | sed 's/;$//; s/;/; /g')"
    : > "$d/texts"
  elif [ -z "$archives" ]; then
    echo "its APKBUILD names no tar source archive"
  elif [ ! -s "$d/texts" ]; then
    echo "no top-level license file in its source archives (${archives# })"
  fi
  return 0
}

# Canonical text for one SPDX id: prints "id|path" (deprecated "+" ids are
# mapped to -or-later), or nothing when no text is available.
spdx_text() {
  sid=$1
  case "$sid" in
    *+) sid=${sid%+}; case "$sid" in GPL-*|LGPL-*|AGPL-*) sid=$sid-or-later ;; esac ;;
  esac
  case "$sid" in *[!A-Za-z0-9.+-]*|'') return 0 ;; esac
  if [ -n "$SPDX_DIR" ] && [ -s "$SPDX_DIR/$sid.txt" ]; then echo "$sid|$SPDX_DIR/$sid.txt"; return 0; fi
  if [ -s "$TMP/spdx-$sid.txt" ]; then echo "$sid|$TMP/spdx-$sid.txt"; return 0; fi
  [ -n "$SPDX_TAG" ] && [ ! -e "$TMP/nospdx-$sid" ] || return 0
  if fetch "$SPDX_URL/$SPDX_TAG/text/$sid.txt" "$TMP/spdx-$sid.txt" && [ -s "$TMP/spdx-$sid.txt" ]; then
    echo "$sid|$TMP/spdx-$sid.txt"
  else
    rm -f "$TMP/spdx-$sid.txt"; : > "$TMP/nospdx-$sid"  # not an SPDX id, or offline
  fi
}

: > "$TMP/entries"; : > "$TMP/missing"; : > "$TMP/ids"; : > "$TMP/origins"
count=0 added=0 requested=0
while IFS='|' read -r pkg ver arch lic src sver commit; do
  count=$((count + 1))
  text="" note=""
  override=$(awk -F'|' -v p="$pkg" '$1 == p { print $2; exit }' "$TMP/overrides")
  canonical=""
  if [ "$override" = canonical ]; then canonical=yes override=""; fi
  if [ -n "$override" ]; then
    if [ "$override" = - ]; then
      text="none needed (see override)"
    elif [ -s "$ROOT$override" ]; then
      text=$override
    else
      echo "$pkg: override text $override is missing or empty" >> "$TMP/missing"
    fi
  elif [ "$KIND" = dpkg ]; then
    if [ -n "$canonical" ]; then
      echo "$pkg: \"canonical\" overrides are for Alpine packages; point at a text instead" >> "$TMP/missing"
    elif [ -s "$ROOT/usr/share/doc/$pkg/copyright" ]; then
      text=/usr/share/doc/$pkg/copyright
    elif [ -s "$ROOT/usr/share/doc/$src/copyright" ]; then
      text=/usr/share/doc/$src/copyright note="same source package"
    else
      echo "$pkg $ver: no /usr/share/doc/$pkg/copyright" >> "$TMP/missing"
    fi
  else
    for dir in "/usr/share/licenses/$pkg" "/usr/share/licenses/$src"; do
      if [ -z "$text" ] && [ -d "$ROOT$dir" ] && [ -n "$(ls -A "$ROOT$dir" 2>/dev/null)" ]; then
        text=$dir/
      fi
    done
    if [ -z "$text" ]; then
      # Public domain needs no text; every other declared license needs its
      # canonical text, and licenses with a notice to reproduce (or not SPDX,
      # such as "custom") the license files of the package's own source.
      ids=$(printf '%s\n' "$lic" | sed 's/[Pp]ublic[ -][Dd]omain/LicenseRef-public-domain/g' |
        tr '()' '  ' | tr ' ' '\n' | grep -v -x -e '' -e AND -e OR -e WITH -e and -e or -e with || true)
      [ -n "$ids" ] || ids=NONE
      unknown="" fetched="" notice=""
      for id in $ids; do
        [ "$id" = LicenseRef-public-domain ] && continue
        complete_license "$id" || notice="$notice $id"
        found=$(spdx_text "$id")
        if [ -n "$found" ]; then echo "$found" >> "$TMP/ids"; fetched=yes; else unknown="$unknown $id"; fi
      done
      if [ -n "$canonical" ] && [ -n "$fetched" ] && [ -z "$unknown" ]; then
        text="canonical SPDX text below (see override)"
      elif [ -n "$canonical" ]; then
        echo "$pkg $ver: \"canonical\" override, but no canonical text for${unknown:- its declaration} (declared: ${lic:-nothing})" >> "$TMP/missing"
      elif [ -n "$notice" ]; then
        why=$(source_texts "$src" "$sver" "$commit")
        if [ -z "$why" ]; then
          text="license files of the $src source${fetched:+ and canonical SPDX texts}, below"
          echo "$src $sver" >> "$TMP/origins"
        else
          echo "$pkg $ver: needs its own notices for${notice}, but $why (declared: ${lic:-nothing})" >> "$TMP/missing"
        fi
      elif [ -n "$unknown" ]; then
        echo "$pkg $ver: no license text for${unknown} (declared: ${lic:-nothing})" >> "$TMP/missing"
      elif [ -n "$fetched" ]; then
        text="canonical SPDX text below"
      else
        text="none needed (declared public domain)"
      fi
    fi
  fi
  if [ "$KIND" = dpkg ] && [ "$DISTRO_ID" = ubuntu ]; then
    url="https://launchpad.net/ubuntu/+source/$src/$sver"
  elif [ "$KIND" = dpkg ]; then
    encoded=$(printf '%s' "$sver" | sed 's/%/%25/g; s/:/%3A/g; s/+/%2B/g')
    url="https://snapshot.debian.org/package/$src/$encoded/"
  elif [ -n "$commit" ]; then
    url="https://gitlab.alpinelinux.org/alpine/aports/-/tree/$commit (*/$src/APKBUILD)"
  else
    url="aports $BRANCH, */$src/APKBUILD"
  fi
  {
    echo "Package: $pkg"
    echo "Version: $ver"
    [ -z "$arch" ] || echo "Architecture: $arch"
    [ -z "$lic" ] || echo "License: $lic"
    echo "Source package: $src $sver"
    echo "Source: $url"
    echo "License text: ${text:-MISSING}${note:+ ($note)}"
    if [ -n "$BASE_DB" ] && ! grep -q -x -F "$pkg" "$TMP/base"; then
      echo "Added: yes"; added=$((added + 1))
      if grep -q -x -F "$pkg" "$TMP/requested"; then echo "Requested: yes"; requested=$((requested + 1)); fi
    fi
    echo
  } >> "$TMP/entries"
done < "$TMP/packages"

cut -d'|' -f1 "$TMP/packages" > "$TMP/names"
cut -d'|' -f1 "$TMP/overrides" | while read -r name; do
  grep -q -x -F "$name" "$TMP/names" || echo "os-packages.sh: warning: override for $name matches no installed package" >&2
done
if [ -s "$TMP/missing" ]; then
  echo "os-packages.sh: these packages have no license text; add the text to the image" >&2
  echo "and an override line, or fix the package selection:" >&2
  sed 's/^/  /' "$TMP/missing" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
{
  echo "OS packages in ${IMAGE:-this image}"
  echo
  echo "${IMAGE:-This image} contains the $DISTRO packages listed below, read from"
  echo "$DB when the image was built. Each keeps its own license."
  if [ "$KIND" = dpkg ]; then
    echo "Each package's copyright and license texts are in the file named on its"
    echo "\"License text\" line. The exact source code of every listed version is at"
    echo "the \"Source\" URL (Debian's snapshot archive, or Launchpad for Ubuntu); the"
    echo "source package and version are also on the \"Source package\" line, for"
    echo "apt-get source."
  else
    echo "Alpine packages do not install license files, so the canonical texts of the"
    echo "licenses they declare follow the list (from SPDX license-list-data"
    echo "${SPDX_TAG:-in $SPDX_DIR}). Where a license requires the package's own copyright or"
    echo "NOTICE lines, the license files of its upstream source follow as well, byte"
    echo "for byte from the source archives its APKBUILD names (checked against their"
    echo "sha512sums). The APKBUILD of every listed version is at the aports commit on"
    echo "its \"Source\" line; the upstream source archives it uses are mirrored at"
    echo "$DISTFILES/$BRANCH/ ."
  fi
  echo
  echo "Packages: $count"
  [ -z "$BASE_DB" ] || echo "Added by the image build: $added (requested by name: $requested)"
  echo
  cat "$TMP/entries"
  if [ -s "$TMP/ids" ]; then
    LC_ALL=C sort -u -t'|' -k1,1 "$TMP/ids" | while IFS='|' read -r id path; do
      echo "$RULE"
      echo "Canonical license text: $id"
      echo
      cat "$path"
      echo
    done
  fi
  # Upstream license files, once per source package; repeated texts are referenced.
  : > "$TMP/seen"
  LC_ALL=C sort -u "$TMP/origins" | while read -r origin over; do
    d=$(source_dir "$origin" "$over")
    while IFS='|' read -r label path; do
      echo "$RULE"
      echo "License file of the $origin source: $label"
      echo
      sum=$(sha256sum "$d/$path" | cut -d' ' -f1)
      first=$(awk -v s="$sum" '$1 == s { sub(/^[^ ]* /, ""); print; exit }' "$TMP/seen")
      if [ -n "$first" ]; then
        echo "Same text as the license file of $first."
      else
        echo "$sum $origin source: $label" >> "$TMP/seen"
        cat "$d/$path"
      fi
      echo
    done < "$d/texts"
  done
} > "$OUT"
echo "os-packages.sh: wrote $OUT ($count packages)" >&2
