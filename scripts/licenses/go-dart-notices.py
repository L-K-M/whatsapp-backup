#!/usr/bin/env python3
"""Write Third-Party-Notices.txt for Go binaries, Dart AOT executables and
hand-listed components (downloaded tools, base-image binaries).

  go    Reads the module list embedded in each Go binary (`go version -m`,
        which survives strip and -trimpath), so only modules that are really
        linked are listed. Adds the Go standard library and runtime from the
        toolchain that built the binary ($(go env GOROOT)/LICENSE, PATENTS).
  dart  Walks `dart pub deps --json` from the executable's package over
        regular (not dev) dependencies; texts come from the resolved package
        directories in .dart_tool/package_config.json. Adds the Dart SDK
        LICENSE and the texts of the third-party code the AOT runtime embeds
        (BoringSSL, ICU, zlib, LLVM libc++/libunwind, Perfetto,
        double-conversion), produced by dart-runtime-licenses.py.
  extra Only the "extra" components of the overrides file, for images that
        add prebuilt tools but no Go or Dart binary.

Each entry: name, version, SPDX license expression, source URL, a Source
Code Form URL for copyleft components, then every LICENSE*, LICENCE*,
COPYING*, NOTICE*, COPYRIGHT*, UNLICENSE*, PATENTS* file verbatim. Entries
are sorted; toolchain runtimes come first. Output is deterministic.

The run fails (exit 1, nothing written) when a component has no license
text, when its license text is not recognised, when a GPL-family text leaves
"-only" versus "-or-later" open, or when a copyleft component has no source
location. Fix gaps with explicit overrides, never by omission:

  // Whole-line // comments are allowed. Every entry needs a "reason".
  {
    "spdx_dir": "spdx",
    "components": [
      {"name": "example.org/gpl-module", "version": "v0.2.*",
       "license": "GPL-3.0-only",
       "reason": "LICENSE is the GPLv3 text; file headers say version 3 only"},
      {"name": "example.com/nolicense", "version": "v1.2.3",
       "spdx": "MIT", "copyright": "Copyright (c) 2020 Jane Doe",
       "reason": "Publishes no license file; README at v1.2.3 says MIT"}
    ],
    "extra": [
      {"name": "example-tool", "version": "${TOOL_VERSION}",
       "license": "GPL-2.0-or-later",
       "source_url": "https://example.org/releases/example-tool-${TOOL_VERSION}.tar.gz",
       "files": ["/licenses/example-tool/COPYING"],
       "reason": "Downloaded release binary, not built here"}
    ]
  }

Override keys: name and version (shell-style globs), reason, license (SPDX
expression that replaces detection), files (extra texts, relative to the
overrides file), spdx (canonical text from spdx_dir/<ID>.txt), copyright
(lines that replace the canonical text's copyright placeholder), source_url,
note, skip (leave the component out; own code only). Extra keys: name, version, reason,
license, files, spdx, copyright, source_url, note. "${VAR}" in extra entries
expands from the environment (Dockerfile ARGs); an unset VAR is an error.

Examples:
  go-dart-notices.py go --binary /out/app --title app -o /out/licenses/Third-Party-Notices.txt
  go-dart-notices.py go --binary /out/tool --main-dir /src/tool \\
      --main-source 'https://github.com/<owner>/<repo>/tree/{revision}' \\
      --overrides licenses/go-overrides.json -o /out/licenses/Third-Party-Notices.txt
  go-dart-notices.py dart --workspace . --package my_server \\
      --runtime-licenses licenses/dart-runtime -o dist/Third-Party-Notices.txt
  go-dart-notices.py extra --overrides licenses/tools.json -o /out/tools-notices.txt

Python 3.8+ standard library only. Needs `go` (go mode) or `dart` (dart
mode) on PATH, the module cache or pub cache the build used, and network
access only when a Go module is missing from the cache.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RULE = "=" * 72

# File names that hold license texts or notices (top level of a component).
TEXT_NAME = re.compile(
    r"^(?:licen[cs]es?|copying|copyright|notices?|unlicense|patents)(?:[-_. 0-9].*)?$"
    r"|^[\w.-]+[-_.](?:licen[cs]es?|copying|notices?)(?:[-_. 0-9].*)?$", re.I)
# Never treat code or data as a license text, even when named license.go.
CODE_SUFFIXES = {
    ".go", ".dart", ".c", ".h", ".cc", ".cpp", ".s", ".py", ".js", ".ts", ".json",
    ".yaml", ".yml", ".toml", ".sh", ".mod", ".sum", ".tmpl", ".png", ".svg",
}
LICENSE_DIRS = {"licenses", "license", "licence", "licences"}  # compared lowercased
# The project's own license file, in order of preference (LICENSE, LICENSE.md, COPYING, ...).
OWN_LICENSE = re.compile(r"^(licen[cs]e|copying|unlicense)(?:\.(?:md|txt|rst))?$", re.I)

# Recognised license texts: (SPDX id, phrases that must all occur). Phrases
# are matched against the text lowercased with punctuation collapsed to
# spaces. Ids ending in "*" are GPL-family texts whose "-only"/"-or-later"
# choice lives in file headers, not in the license text: they need an
# override with the exact id.
SIGNATURES = [
    ("Apache-2.0 WITH LLVM-exception", ["apache license", "llvm exceptions"]),
    ("Apache-2.0", ["apache license", "version 2 0"]),
    ("Apache-1.1", ["the apache software license version 1 1"]),
    ("MPL-2.0", ["mozilla public license version 2 0"]),
    ("MPL-2.0", ["mozilla public license v 2 0"]),
    ("AGPL-3.0*", ["gnu affero general public license version 3 19 november 2007"]),
    ("LGPL-3.0*", ["gnu lesser general public license version 3 29 june 2007"]),
    ("LGPL-2.1*", ["gnu lesser general public license version 2 1 february 1999"]),
    ("LGPL-2.0*", ["gnu library general public license version 2 june 1991"]),
    ("GPL-3.0*", ["gnu general public license version 3 29 june 2007"]),
    ("GPL-2.0*", ["gnu general public license version 2 june 1991"]),
    ("EPL-2.0", ["eclipse public license v 2 0"]),
    ("Unlicense", ["this is free and unencumbered software released into the public domain"]),
    ("CC0-1.0", ["cc0 1 0 universal"]),
    ("BSL-1.0", ["boost software license version 1 0"]),
    ("Zlib", ["provided as is without any express or implied warranty",
              "altered source versions must be plainly marked as such"]),
    ("blessing", ["may you do good and not evil"]),
    ("OFL-1.1", ["sil open font license", "version 1 1"]),
    ("Unicode-3.0", ["unicode license v3"]),
    ("Unicode-DFS-2016", ["unicode inc license agreement data files and software"]),
    ("CC-BY-4.0", ["attribution 4 0 international", "creative commons"]),
    ("WTFPL", ["do what the fuck you want to public license"]),
    ("NCSA", ["university of illinois ncsa open source license"]),
    ("OpenSSL", ["this product includes software developed by the openssl project"]),
    ("BSD-4-Clause", ["redistribution and use in source and binary forms",
                      "all advertising materials mentioning"]),
    # Clause 3 is worded many ways ("neither the name", "shall not be used to
    # endorse or promote"); BSD-2 needs both of its conditions, so a text that
    # matches neither form is reported instead of labelled BSD-2-Clause.
    ("BSD-3-Clause", ["redistribution and use in source and binary forms", "neither the name"]),
    ("BSD-3-Clause", ["redistribution and use in source and binary forms", "endorse or promote"]),
    ("BSD-2-Clause", ["redistribution and use in source and binary forms",
                      "redistributions of source code must retain", "redistributions in binary form must reproduce"]),
    ("BSD-2-Clause", ["redistribution and use in source and binary forms",
                      "redistribution of source code must retain", "redistribution in binary form must reproduce"]),
    ("curl", ["permission to use copy modify and distribute this software for any purpose",
              "the name of a copyright holder shall not be used in advertising"]),
    ("ISC", ["permission to use copy modify and or distribute this software for any purpose",
             "copyright notice and this permission notice appear in all copies"]),
    ("ISC", ["permission to use copy modify and distribute this software for any purpose",
             "copyright notice and this permission notice appear in all copies"]),
    ("0BSD", ["permission to use copy modify and or distribute this software for any purpose"]),
    ("MIT", ["permission is hereby granted free of charge to any person obtaining a copy",
             "copyright notice and this permission notice shall be included"]),
    ("MIT-0", ["permission is hereby granted free of charge to any person obtaining a copy"]),
]
# A match of the key suppresses these weaker matches of the same text.
SUPPRESSES = {
    "Apache-2.0 WITH LLVM-exception": {"Apache-2.0"},
    "LGPL-3.0*": {"GPL-3.0*"},  # LGPLv3 is GPLv3 plus permissions; packages ship both texts
    "OpenSSL": {"BSD-4-Clause", "BSD-3-Clause", "BSD-2-Clause"},
    "Apache-1.1": {"BSD-4-Clause", "BSD-3-Clause", "BSD-2-Clause"},
    "BSD-4-Clause": {"BSD-3-Clause", "BSD-2-Clause"},
    "BSD-3-Clause": {"BSD-2-Clause"},
    "ISC": {"0BSD"},
    "curl": {"ISC", "0BSD"},
    "MIT": {"MIT-0"},
}
# Copyright placeholders in canonical SPDX templates.
PLACEHOLDER = re.compile(r"<\s*(?:year|owner|copyright holders?|name of author|organization)\s*>"
                         r"|\[\s*(?:year|yyyy|fullname|name of copyright owner)\s*\]", re.I)
# Licenses whose binary distribution needs a pointer to the exact source.
COPYLEFT = re.compile(r"\b(?:(?:MPL|LGPL|GPL|AGPL|EPL|CDDL|EUPL|OSL|CPL|CC-BY-SA)-|MS-RL\b)", re.I)

OVERRIDE_KEYS = {"name", "version", "reason", "license", "files", "spdx", "copyright",
                 "source_url", "note", "skip"}
EXTRA_KEYS = {"name", "version", "reason", "license", "files", "spdx", "copyright",
              "source_url", "note"}


class Problems:
    """Collects every problem so one run reports all of them."""

    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def finish(self):
        for message in self.warnings:
            print(f"warning: {message}", file=sys.stderr)
        for message in self.errors:
            print(f"error: {message}", file=sys.stderr)
        if self.errors:
            fail(f"{len(self.errors)} problem(s); nothing written")


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def run(command, cwd=None, env=None):
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, encoding="utf-8")
    except FileNotFoundError:
        fail(f"{command[0]} is not on PATH")
    if result.returncode != 0:
        fail(f"{' '.join(command)} failed:\n{result.stderr.strip()}")
    return result.stdout


def read_text(path):
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n") + "\n"


def normalized(text):
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "


def classify(text):
    """Return the set of SPDX ids whose signature phrases all occur in text."""
    flat = normalized(text)
    found = set()
    for identifier, phrases in SIGNATURES:
        if all(" " + phrase + " " in flat for phrase in phrases):
            found.add(identifier)
    for strong, weak in SUPPRESSES.items():
        if strong in found:
            found -= weak
    if len(found) > 1:
        found.discard("MIT-0")  # its grant sentence also opens OFL, Unicode and NCSA texts
    return found


def license_files(directory):
    """Top-level license and notice files of a component, plus licenses/ dirs."""
    directory = Path(directory)
    if not directory.is_dir():
        fail(f"{directory} is not a directory")
    found = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.is_file() and TEXT_NAME.match(path.name) and path.suffix.lower() not in CODE_SUFFIXES:
            if path.stat().st_size:
                found.append(path)
        elif path.is_dir() and path.name.lower() in LICENSE_DIRS:
            found.extend(p for p in sorted(path.rglob("*")) if p.is_file() and p.stat().st_size
                         and p.suffix.lower() not in CODE_SUFFIXES)
    return found


def expand(value, label):
    """Expand ${VAR} from the environment in extra entries; unset is an error."""
    if isinstance(value, list):
        return [expand(item, label) for item in value]
    if not isinstance(value, str):
        return value

    def substitute(match):
        name = match.group(1)
        if name not in os.environ:
            fail(f"{label}: ${{{name}}} is not set in the environment")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)


def as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def load_overrides(path):
    if not path:
        return {"components": [], "extra": [], "base": Path.cwd(), "spdx_dir": Path.cwd() / "spdx"}
    path = Path(path)
    source = "\n".join(line for line in path.read_text(encoding="utf-8").split("\n")
                       if not re.match(r"^\s*//", line))
    try:
        document = json.loads(source)
    except ValueError as error:
        fail(f"{path}: {error}")
    if not isinstance(document, dict):
        fail(f"{path}: expected a JSON object")
    for section in ("components", "extra"):
        if not isinstance(document.get(section, []), list) or not all(
                isinstance(entry, dict) for entry in document.get(section, [])):
            fail(f"{path}: \"{section}\" must be a list of objects")
    unknown = set(document) - {"spdx_dir", "components", "extra"}
    if unknown:
        fail(f"{path}: unknown top-level keys {sorted(unknown)}")
    for section, allowed in (("components", OVERRIDE_KEYS), ("extra", EXTRA_KEYS)):
        for entry in document.get(section, []):
            label = f"{path}: {section} entry {entry.get('name')!r}"
            if not entry.get("name") or not str(entry.get("reason", "")).strip():
                fail(f"{label} needs a name and a non-empty reason")
            if set(entry) - allowed:
                fail(f"{label} has unknown keys {sorted(set(entry) - allowed)}")
    base = path.parent
    return {"components": document.get("components", []), "extra": document.get("extra", []),
            "base": base, "spdx_dir": base / document.get("spdx_dir", "spdx")}


def override_for(overrides, name, version):
    matches = [entry for entry in overrides["components"]
               if fnmatch.fnmatchcase(name, entry["name"])
               and fnmatch.fnmatchcase(version or "", entry.get("version", "*"))]
    if len(matches) > 1:
        fail(f"several overrides match {name} {version}")
    if matches:
        matches[0]["_used"] = True
        return matches[0]
    return {}


def override_texts(entry, overrides, problems, label):
    """Texts an override or extra entry supplies: canonical SPDX texts and files."""
    texts = []
    copyright_lines = as_list(entry.get("copyright"))
    for identifier in as_list(entry.get("spdx")):
        path = overrides["spdx_dir"] / f"{identifier}.txt"
        if not path.is_file():
            problems.error(f"{label}: canonical text {path} is missing (vendor it from the SPDX "
                           f"license-list-data release, e.g. with the rust recipe's fetch-spdx-texts.py)")
            continue
        text = read_text(path)
        if copyright_lines:
            # Put the real copyright line where the template has its placeholder.
            lines = text.split("\n")
            spots = [i for i, line in enumerate(lines) if PLACEHOLDER.search(line)]
            if spots:
                lines[spots[0]:spots[0] + 1] = copyright_lines
                text = "\n".join(lines)
            else:
                text = "\n".join(copyright_lines) + "\n\n" + text
        texts.append((f"{identifier} (canonical SPDX text, supplied by override)", text))
    for relative in as_list(entry.get("files")):
        path = (overrides["base"] / relative).resolve() if not os.path.isabs(relative) else Path(relative)
        if not path.is_file() or not path.stat().st_size:
            problems.error(f"{label}: override file {path} is missing or empty")
            continue
        texts.append((f"{path.name} (supplied by override)", read_text(path)))
    return texts


def render(name, version, expression, source=None, source_form=None, notes=(), texts=()):
    header = [RULE, f"{name} {version}".strip(), f"License: {expression}"]
    if source:
        header.append(f"Source: {source}")
    if source_form:
        header.append(f"Source Code Form: {source_form}")
    header.extend(f"Note: {note}" for note in notes if note)
    body = "".join(f"\n--- {shown} ---\n\n{text}" for shown, text in texts)
    return "\n".join(header) + "\n" + body


def component(problems, overrides, name, version, directory, source=None, source_form=None, notes=()):
    """Build one entry from a component directory, applying its override."""
    label = f"{name} {version}".strip()
    override = override_for(overrides, name, version)
    if override.get("skip"):
        print(f"skipped by override: {label}: {override['reason']}", file=sys.stderr)
        return None
    texts, ids = [], set()
    if directory is not None:
        root = Path(directory)
        for path in license_files(root):
            text = read_text(path)
            texts.append((str(path.relative_to(root)), text))
            ids |= classify(text)
    supplied = override_texts(override, overrides, problems, label)
    for shown, text in supplied:
        ids |= classify(text)
    ids |= set(as_list(override.get("spdx")))  # canonical texts name their id
    texts += supplied
    if not texts:
        problems.error(f"{label} has no license text in {directory}; add an override with "
                       f"\"files\" or \"spdx\" and a reason")
        return None
    expression = override.get("license")
    if not expression:
        ambiguous = sorted(i for i in ids if i.endswith("*"))
        if ambiguous:
            problems.error(f"{label}: {', '.join(i[:-1] for i in ambiguous)} text found; check the "
                           f"source file headers and set \"license\" to the exact -only/-or-later id")
            return None
        if not ids:
            problems.error(f"{label}: license text not recognised ({', '.join(s for s, _ in texts)}); "
                           f"set \"license\" in an override")
            return None
        expression = " AND ".join(sorted(ids))
    source = override.get("source_url") or source
    if COPYLEFT.search(expression):
        source_form = override.get("source_url") or source_form
        if not source_form:
            problems.error(f"{label} is under {expression} but has no source location; "
                           f"add \"source_url\" in its override")
    else:
        source_form = None
    notes = list(notes) + as_list(override.get("note"))
    return (name.lower(), version or ""), render(name, version, expression, source, source_form, notes, texts)


def extras(problems, overrides):
    entries = []
    for raw in overrides["extra"]:
        entry = {key: expand(value, f"extra {raw['name']}") for key, value in raw.items()}
        label = f"{entry['name']} {entry.get('version', '')}".strip()
        texts = override_texts(entry, overrides, problems, label)
        if not texts:
            problems.error(f"extra {label} needs \"files\" or \"spdx\"")
            continue
        if not entry.get("license"):
            problems.error(f"extra {label} needs \"license\"")
            continue
        form = None
        if COPYLEFT.search(entry["license"]):
            form = entry.get("source_url")
            if not form:
                problems.error(f"extra {label} is under {entry['license']} and needs \"source_url\"")
        entries.append(((entry["name"].lower(), entry.get("version", "")),
                        render(entry["name"], entry.get("version", ""), entry["license"],
                               entry.get("source_url"), form, as_list(entry.get("note")), texts)))
    return entries


def write(args, title, intro, runtime, entries, problems, overrides):
    for entry in overrides["components"]:
        if not entry.pop("_used", False):
            problems.warn(f"override {entry['name']} {entry.get('version', '*')} matched no component")
    problems.finish()
    entries = sorted(entries)
    document = (f"Third-party notices for {title}\n\n{intro}"
                + "".join(f"\n{note}\n" for note in args.note)
                + f"\nComponents: {len(runtime) + len(entries)}\n"
                + "".join(f"\n{text}" for text in runtime)
                + "".join(f"\n{text}" for _, text in entries))
    for path in args.append:
        if not Path(path).is_file() or not Path(path).stat().st_size:
            fail(f"--append file {path} is missing or empty")
        document += f"\n{RULE}\nIncluded from {Path(path).name}\n{RULE}\n\n" + read_text(path)
    if args.output == "-":
        sys.stdout.write(document)
    else:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8")
        count = len(runtime) + len(entries)
        print(f"wrote {out} ({count} component{'' if count == 1 else 's'})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

def escape(path):
    """Module cache and proxy escaping: uppercase letters become !lowercase."""
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), path)


def buildinfo(binary):
    """Parse `go version -m` output: Go version, main module, deps, settings."""
    lines = run(["go", "version", "-m", str(binary)]).splitlines()
    match = re.search(r": (go\S+)", lines[0]) if lines else None
    if not match:
        fail(f"{binary}: no Go build information")
    info = {"go": match.group(1), "main": None, "deps": [], "settings": {}}
    for line in lines[1:]:
        fields = line.strip().split("\t")
        kind, rest = fields[0], fields[1:] + ["", ""]
        if kind == "mod":
            info["main"] = (rest[0], rest[1])
        elif kind == "dep":
            info["deps"].append({"path": rest[0], "version": rest[1]})
        elif kind == "=>" and info["deps"]:
            info["deps"][-1]["replace"] = (rest[0], rest[1])
        elif kind == "build":
            key, _, value = rest[0].partition("=")
            info["settings"][key] = value
    return info


def module_dirs(modules):
    """Directory of each (path, version) in the module cache; downloads misses."""
    cache = run(["go", "env", "GOMODCACHE"]).strip()
    dirs, missing = {}, []
    for path, version in modules:
        candidate = Path(cache) / f"{escape(path)}@{escape(version)}"
        if candidate.is_dir():
            dirs[(path, version)] = candidate
        else:
            missing.append((path, version))
    if missing:
        env = dict(os.environ, GOFLAGS="", GOWORK="off", GOTOOLCHAIN="local")
        with tempfile.TemporaryDirectory() as scratch:
            output = run(["go", "mod", "download", "-json"] + [f"{p}@{v}" for p, v in missing],
                         cwd=scratch, env=env)
        decoder, position = json.JSONDecoder(), 0
        while position < len(output.rstrip()):
            item, end = decoder.raw_decode(output, position)
            position = end
            while position < len(output) and output[position].isspace():
                position += 1
            if item.get("Error") or not item.get("Dir"):
                fail(f"go mod download {item.get('Path')}@{item.get('Version')}: {item.get('Error')}")
            dirs[(item["Path"], item["Version"])] = Path(item["Dir"])
    return dirs


def go_main(args):
    problems, overrides = Problems(), load_overrides(args.overrides)
    infos = [buildinfo(binary) for binary in args.binary]
    versions = sorted({info["go"] for info in infos})
    if len(versions) != 1:
        fail(f"binaries were built with different Go versions: {versions}")
    goversion = versions[0]
    toolchain = run(["go", "env", "GOVERSION"]).split()[0]
    if toolchain != goversion:
        fail(f"binaries were built with {goversion} but `go` is {toolchain}; run this with the "
             f"toolchain that built them, so its standard library notices are the ones shipped")
    for binary, info in zip(args.binary, infos):
        if info["settings"].get("CGO_ENABLED") == "1":
            problems.warn(f"{binary} uses cgo: C libraries linked statically (libc with -static, "
                          f"vendored C code outside Go modules) need their own entries via \"extra\"")
        if "boringcrypto" in info["settings"].get("GOEXPERIMENT", ""):
            problems.warn(f"{binary} embeds BoringCrypto: add BoringSSL's notices via \"extra\"")

    goroot = Path(run(["go", "env", "GOROOT"]).strip())
    runtime_texts = []
    for name in ("LICENSE", "PATENTS"):
        path = goroot / name
        if name == "LICENSE" and (not path.is_file() or not path.stat().st_size):
            fail(f"{path} is missing: cannot ship the Go standard library notices")
        if path.is_file():
            runtime_texts.append((name, read_text(path)))
    runtime = [render("Go standard library and runtime", goversion,
                      " AND ".join(sorted(classify(runtime_texts[0][1]))) or "BSD-3-Clause",
                      f"https://go.dev/dl/{goversion}.src.tar.gz", None,
                      ["Statically linked into every Go binary."], runtime_texts)]

    modules = {}
    for info in infos:
        for dep in info["deps"]:
            path, version = dep.get("replace") or (dep["path"], dep["version"])
            modules[(path, version)] = dep
    local = {key: dep for key, dep in modules.items() if not key[1] or key[1] == "(devel)"}
    root = Path(args.module_root or args.main_dir or Path.cwd())
    own_license = own_license_file(args.own_license, root)
    dirs = module_dirs([key for key in modules if key not in local])

    entries = []
    for (path, version), dep in sorted(modules.items()):
        notes = []
        if dep.get("replace"):
            notes.append(f"Replaces {dep['path']} {dep['version']}.")
        if (path, version) in local:
            # A replace directive pointing at a directory: resolve it from the main module.
            directory = (root / path).resolve()
            if not directory.is_dir():
                problems.error(f"{dep['path']} is replaced by local directory {path}, not found from "
                               f"{root}; pass --module-root or add an override")
                continue
            files = license_files(directory)
            if files and same_text(files, own_license) and not override_for(overrides, dep["path"], "local"):
                print(f"note: {dep['path']} ({path}) carries the project's LICENSE; treated as own code",
                      file=sys.stderr)
                continue
            # Anything else needs its texts, or a "skip" override when it is own code.
            entry = component(problems, overrides, dep["path"], "local", directory,
                              notes=notes + [f"Built from the local directory {path}."])
        else:
            url = f"https://proxy.golang.org/{escape(path)}/@v/{escape(version)}.zip"
            entry = component(problems, overrides, path, version, dirs[(path, version)],
                              source=f"https://pkg.go.dev/{path}@{version}", source_form=url, notes=notes)
        if entry:
            entries.append(entry)

    if args.main_dir:
        # The main module is third-party here (an upstream program built by this repo).
        if not infos[0]["main"]:
            fail(f"{args.binary[0]}: no main module in the build information")
        if any(info["main"] != infos[0]["main"] for info in infos):
            fail("--main-dir describes one main module, but the binaries have different ones; "
                 "run the generator once per main module and concatenate the files")
        if not Path(args.main_dir).is_dir():
            fail(f"--main-dir {args.main_dir} is not a directory")
        main_path, main_version = infos[0]["main"]
        settings = infos[0]["settings"]
        revision = settings.get("vcs.revision", "")
        modified = settings.get("vcs.modified") == "true" or main_version.endswith("+dirty")
        released = main_version not in ("", "(devel)") and not modified
        version = args.main_version or (main_version if main_version not in ("", "(devel)") else revision)
        if not version:
            fail(f"{main_path}: no version or VCS revision in the binary; pass --main-version")
        notes = [f"Built from VCS revision {revision}." if revision else ""]
        if modified and not args.main_note:
            problems.error(f"{main_path} was built from a modified tree; say how with --main-note")
        source = (args.main_source or "").format(revision=revision or version, version=version) or None
        if not source and released:
            source = f"https://proxy.golang.org/{escape(main_path)}/@v/{escape(main_version)}.zip"
        entry = component(problems, overrides, main_path, version, args.main_dir, source=source,
                          source_form=source, notes=notes + as_list(args.main_note))
        if entry:
            entries.append(entry)

    entries += extras(problems, overrides)
    title = args.title or ", ".join(Path(b).name for b in args.binary)
    intro = (f"{title} is built with Go and links the Go standard library and the Go modules\n"
             f"listed below, read from the binary's embedded module list. Each keeps its own\n"
             f"license; its license and notice files follow verbatim. Source archives of the\n"
             f"exact module versions are on the Go module proxy (Source Code Form lines).\n")
    write(args, title, intro, runtime, entries, problems, overrides)


# ---------------------------------------------------------------------------
# Dart
# ---------------------------------------------------------------------------

def find_dart_sdk(explicit):
    if explicit:
        sdk = Path(explicit)
        if not (sdk / "version").is_file() or not (sdk / "LICENSE").is_file():
            fail(f"--dart-sdk {sdk}: no version and LICENSE files there")
        return sdk
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("dart", "dart.exe", "dart.bat"):
            executable = Path(directory) / name
            if executable.is_file():
                root = executable.resolve().parent.parent
                # Flutter's bin/dart wraps the SDK in bin/cache/dart-sdk; its own
                # root also has version and LICENSE files, so check the cache first.
                for candidate in (root / "bin" / "cache" / "dart-sdk", root):
                    if ((candidate / "version").is_file() and (candidate / "LICENSE").is_file()
                            and (candidate / "lib" / "_internal").is_dir()):
                        return candidate
    fail("Dart SDK not found; pass --dart-sdk")


def lock_descriptions(lock_path):
    """Minimal pubspec.lock reader: package -> {source, url, ref, resolved-ref, path}."""
    packages, current, section = {}, None, None
    if not lock_path.is_file():
        return packages
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"')
        if indent == 0:
            section = key
        elif section == "packages" and indent == 2:
            current = packages.setdefault(key, {})
        elif section == "packages" and current is not None and indent >= 4 and value:
            current[key] = value
    return packages


def pub_source(name, version, kind, lock):
    """Source and Source Code Form URLs for a pub package."""
    info = lock.get(name, {})
    if kind == "hosted":
        host = info.get("url", "https://pub.dev").rstrip("/")
        return f"{host}/packages/{name}/versions/{version}", f"{host}/api/archives/{name}-{version}.tar.gz"
    if kind == "git" and info.get("url"):
        ref = info.get("resolved-ref", info.get("ref", ""))
        subdir = info.get("path", ".")
        where = f"{info['url']} (git, commit {ref}{'' if subdir in ('', '.') else ', path ' + subdir})"
        return where, where
    return None, None


def own_license_file(explicit, directory):
    """--own-license, else the project's LICENSE/LICENSE.md/COPYING/UNLICENSE in directory."""
    if explicit:
        if not Path(explicit).is_file():
            fail(f"--own-license {explicit} is not a file")
        return Path(explicit)
    directory = Path(directory)
    if not directory.is_dir():
        return None
    rank = {"license": 0, "licence": 0, "copying": 1, "unlicense": 2}
    found = sorted((rank[m.group(1).lower()], path.name, path) for path in directory.iterdir()
                   for m in [OWN_LICENSE.match(path.name)] if m and path.is_file())
    return found[0][2] if found else None


def same_text(paths, reference):
    if not reference:
        return False
    wanted = normalized(read_text(reference))
    return bool(paths) and all(normalized(read_text(path)) == wanted for path in paths)


def dart_main(args):
    problems, overrides = Problems(), load_overrides(args.overrides)
    workspace = Path(args.workspace).resolve()
    config_path = workspace / ".dart_tool" / "package_config.json"
    if not config_path.is_file():
        fail(f"{config_path} is missing; run `dart pub get --enforce-lockfile` first")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    roots = {}
    for package in config["packages"]:
        uri = package["rootUri"]
        if uri.startswith("file://"):
            from urllib.parse import unquote, urlparse
            location = Path(unquote(urlparse(uri).path))
            if os.name == "nt" and str(location).startswith("\\"):
                location = Path(str(location).lstrip("\\"))
        else:
            location = (config_path.parent / uri).resolve()
        roots[package["name"]] = location
    deps = json.loads(run(["dart", "pub", "deps", "--json"], cwd=str(workspace)))
    packages = {package["name"]: package for package in deps["packages"]}
    lock = lock_descriptions(workspace / "pubspec.lock")
    own_license = own_license_file(args.own_license, workspace)

    # Regular dependencies only: workspace members list dev ones separately.
    reachable, pending = set(), list(args.package)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        if name not in packages:
            fail(f"package {name} is not in `dart pub deps` of {workspace}")
        reachable.add(name)
        package = packages[name]
        key = "directDependencies" if package.get("kind") == "root" else "dependencies"
        pending.extend(package.get(key, []))

    entries = []
    for name in sorted(reachable):
        package = packages[name]
        kind, version = package.get("source"), package.get("version", "")
        directory = roots.get(name)
        if kind == "sdk":
            problems.warn(f"{name} comes from an SDK (Flutter?); Flutter apps ship NOTICES instead")
            continue
        if kind in ("root", "path"):
            files = license_files(directory) if directory and directory.is_dir() else []
            if name in args.own or not files or same_text(files, own_license):
                if kind == "path" and not files and name not in args.own:
                    print(f"note: path package {name} has no license file; treated as own code",
                          file=sys.stderr)
                continue
            entry = component(problems, overrides, name, version, directory,
                              notes=[f"Vendored in this repository ({directory.name})."])
        else:
            if not directory or not directory.is_dir():
                problems.error(f"{name} {version}: package directory {directory} not found")
                continue
            source, form = pub_source(name, version, kind, lock)
            entry = component(problems, overrides, name, version, directory, source=source,
                              source_form=form)
        if entry:
            entries.append(entry)

    # The Dart SDK: core libraries compiled into the snapshot plus the AOT runtime.
    sdk = find_dart_sdk(args.dart_sdk)
    sdk_version = (sdk / "version").read_text(encoding="utf-8").strip()
    revision_file = sdk / "revision"
    revision = revision_file.read_text(encoding="utf-8").strip() if revision_file.is_file() else ""
    sdk_text = read_text(sdk / "LICENSE")
    runtime = [render("Dart SDK (runtime and core libraries)", sdk_version,
                      " AND ".join(sorted(classify(sdk_text))) or "BSD-3-Clause",
                      f"https://github.com/dart-lang/sdk/tree/{revision or sdk_version}", None,
                      ["Statically linked into every executable built by `dart compile exe`."],
                      [("LICENSE", sdk_text)])]
    if not args.runtime_licenses:
        fail("--runtime-licenses is required: the AOT runtime embeds BoringSSL, ICU, zlib, LLVM "
             "libc++/libunwind, Perfetto and double-conversion; create the directory with "
             "dart-runtime-licenses.py")
    runtime_dir = Path(args.runtime_licenses)
    manifest_path = runtime_dir / "components.json"
    if not manifest_path.is_file():
        fail(f"{manifest_path} is missing; create it with dart-runtime-licenses.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Exact match: DEPS pins (BoringSSL, ICU, LLVM revisions) can move in a patch release.
    captured, captured_revision = manifest.get("sdk_version", ""), manifest.get("sdk_revision", "")
    if captured != sdk_version or (revision and captured_revision != revision):
        fail(f"{runtime_dir} was captured for Dart {captured} ({captured_revision or 'no revision'}), "
             f"but the SDK is {sdk_version} ({revision or 'no revision file'}); "
             f"refresh it with dart-runtime-licenses.py --sdk {sdk}")
    for item in manifest["components"]:
        texts = []
        for name in item["files"]:
            path = runtime_dir / name
            if not path.is_file() or not path.stat().st_size:
                fail(f"{path} is missing or empty")
            texts.append((name, read_text(path)))
        runtime.append(render(item["name"], item["version"], item["license"], item["source_url"],
                              item["source_url"] if COPYLEFT.search(item["license"]) else None,
                              as_list(item.get("note")), texts))

    entries += extras(problems, overrides)
    title = args.title or ", ".join(args.package)
    intro = (f"{title} is a Dart executable compiled ahead of time. It contains the Dart SDK's\n"
             f"runtime and core libraries, the third-party code that runtime embeds, and the\n"
             f"pub packages listed below. Each keeps its own license; its license and notice\n"
             f"files follow verbatim.\n")
    write(args, title, intro, runtime, entries, problems, overrides)


def extra_main(args):
    problems, overrides = Problems(), load_overrides(args.overrides)
    if overrides["components"]:
        fail("extra mode reads only the \"extra\" section; component overrides would match nothing")
    entries = extras(problems, overrides)
    if not entries:
        fail("no extra components")
    title = args.title or "this image"
    intro = (f"{title} includes the prebuilt third-party programs listed below. Each keeps\n"
             f"its own license; its license and notice files follow verbatim.\n")
    write(args, title, intro, [], entries, problems, overrides)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-o", "--output", required=True, help="notices file to write, or - for stdout")
    common.add_argument("--overrides", help="JSON overrides and extra components")
    common.add_argument("--title", help="artifact name for the header")
    common.add_argument("--note", action="append", default=[], help="paragraph for the header (repeatable)")
    common.add_argument("--append", action="append", default=[],
                        help="append another generated notices file verbatim (repeatable)")

    go = sub.add_parser("go", parents=[common], help="Go binaries")
    go.add_argument("--binary", action="append", required=True, help="built Go binary (repeatable)")
    go.add_argument("--main-dir", help="include the main module as a third-party component, "
                    "reading its license from this source directory")
    go.add_argument("--module-root", help="main module directory, for local replace directives "
                    "(default: --main-dir, else the current directory)")
    go.add_argument("--own-license", help="project license file; local modules carrying the same "
                    "text are own code (default: LICENSE, LICENSE.md, COPYING or UNLICENSE in MODULE_ROOT)")
    go.add_argument("--main-version", help="main module version when the binary has none")
    go.add_argument("--main-source", help="main module source URL; {revision} and {version} expand")
    go.add_argument("--main-note", action="append", help="note for the main module (repeatable)")

    dart = sub.add_parser("dart", parents=[common], help="Dart AOT executables")
    dart.add_argument("--workspace", required=True, help="pub workspace or package root (resolved)")
    dart.add_argument("--package", action="append", required=True,
                      help="package that holds the executable's entry point (repeatable)")
    dart.add_argument("--runtime-licenses", help="directory written by dart-runtime-licenses.py")
    dart.add_argument("--dart-sdk", help="Dart SDK root (default: from `dart` on PATH)")
    dart.add_argument("--own", action="append", default=[], help="path package that is own code")
    dart.add_argument("--own-license", help="project license file (default: LICENSE, LICENSE.md, "
                      "COPYING or UNLICENSE in WORKSPACE)")

    sub.add_parser("extra", parents=[common], help="only the overrides file's extra components")

    args = parser.parse_args()
    {"go": go_main, "dart": dart_main, "extra": extra_main}[args.mode](args)


if __name__ == "__main__":
    main()
