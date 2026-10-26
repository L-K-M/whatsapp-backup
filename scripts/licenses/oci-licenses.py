#!/usr/bin/env python3
"""Compute and check an image's org.opencontainers.image.licenses label.

The label must name the license of the project's own code and every license
of the third-party code the image build adds: binaries compiled in the build
(Go modules, Dart packages, Rust crates and their toolchain runtimes), web
bundles, downloaded tools, and the OS packages the Dockerfile installs
explicitly. The base image's own packages are not listed; they are
documented in OS-Packages.txt and keep their own notices.

Most terms come from the "License:" header lines of the generated notices
files (the format of go-dart-notices.py and the rust and js recipes: a rule
line of "=", a name line, then "License: <SPDX expression>"). OS packages
come from OS-Packages.txt (os-packages.sh --base-db; files written without
it are refused): every package marked "Requested: yes" needs a license
term. Alpine packages bring their declared license; Debian ones, and Alpine
ones whose declaration is not valid SPDX, need --package NAME=EXPR (or
NAME=none for public-domain data). Anything else no generated file carries
goes in --add. Comment each --package and --add in the workflow.

  oci-licenses.py expr --own Unlicense --os-packages OS-Packages.txt \\
      --package tzdata=none Third-Party-Notices.txt
  oci-licenses.py check --own Unlicense --os-packages OS-Packages.txt --package tini=MIT \\
      --label "$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.licenses" }}' IMG)" \\
      Third-Party-Notices.txt
  oci-licenses.py dockerfile Dockerfile     # print the final stage's LABEL value

The label is compared as a set of top-level AND terms (OR alternatives are
order-insensitive), so it must contain every required term and nothing else.
Exit 1 on a mismatch, 2 on usage errors. Python 3.8+ standard library only.
"""

import argparse
import re
import sys
from pathlib import Path

LABEL = "org.opencontainers.image.licenses"
IDENTIFIER = re.compile(r"^(?:(?:DocumentRef-[A-Za-z0-9.-]+:)?LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9.-]+\+?)$")
# Distribution shorthands that look like identifiers but are not SPDX ids.
NOT_SPDX = re.compile(r"^(?:public-?domain|public|domain|pd|custom|other|unknown|distributable|"
                      r"bsd|gpl|lgpl|agpl|mpl|apache|artistic|(?:l|a)?gplv\d.*|expat)$", re.I)


def fail(message, code=1):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def parse(expression):
    """SPDX expression -> tree of ('AND'|'OR', [nodes]) and identifier strings."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        if position >= len(tokens):
            raise ValueError("unexpected end")
        position += 1
        return tokens[position - 1]

    def primary():
        token = take()
        if token == "(":
            node = disjunction()
            if take() != ")":
                raise ValueError("missing )")
            return node
        if token.upper() in ("AND", "OR", "WITH") or not IDENTIFIER.match(token) or NOT_SPDX.match(token):
            raise ValueError(f"not an SPDX identifier: {token}")
        if (peek() or "").upper() == "WITH":
            take()
            exception = take()
            if not IDENTIFIER.match(exception):
                raise ValueError(f"not an SPDX exception: {exception}")
            return f"{token} WITH {exception}"
        return token

    def chain(operator, operand):
        nodes = [operand()]
        while (peek() or "").upper() == operator:
            take()
            nodes.append(operand())
        return nodes[0] if len(nodes) == 1 else (operator, nodes)

    def conjunction():
        return chain("AND", primary)

    def disjunction():
        return chain("OR", conjunction)

    node = disjunction()
    if position != len(tokens):
        raise ValueError(f"unexpected {tokens[position]}")
    return node


def canonical(node, top=False):
    """Deterministic text for a node; nested operators are flattened and sorted."""
    if isinstance(node, str):
        return node
    operator, children = node
    flat = []
    for child in children:
        if not isinstance(child, str) and child[0] == operator:
            flat.extend(child[1])
        else:
            flat.append(child)
    text = f" {operator} ".join(sorted({canonical(child) for child in flat}))
    return text if top else f"({text})"


def terms(expression, where):
    """Set of canonical top-level AND terms of one expression."""
    try:
        node = parse(expression)
    except ValueError as error:
        fail(f"{where}: {expression!r} is not an SPDX expression ({error})")
    children = node[1] if not isinstance(node, str) and node[0] == "AND" else [node]
    result = set()
    for child in children:
        if not isinstance(child, str) and child[0] == "AND":
            result |= terms(canonical(child, top=True), where)
        else:
            result.add(canonical(child))
    return result


def notice_licenses(path):
    """License expressions from entry headers: rule line, name line, License: line."""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
    found = []
    for index in range(2, len(lines)):
        if (lines[index].startswith("License: ") and re.fullmatch(r"={20,}", lines[index - 2])
                and lines[index - 1].strip()):
            expression = lines[index][len("License: "):].strip()
            if expression.upper() in ("NOASSERTION", "NONE", "UNKNOWN", "SEE BELOW", ""):
                fail(f"{path}: {lines[index - 1]!r} has no SPDX license ({expression!r}); fix its entry")
            found.append((lines[index - 1], expression))
    if not found:
        fail(f"{path}: no entries with License: lines")
    return found


def requested_packages(path):
    """(name, declared license or None) for OS-Packages.txt stanzas marked Requested."""
    # Upstream license files appended on Alpine are verbatim and not always UTF-8.
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    # Without --base-db nothing is marked, and every OS package would drop out unnoticed.
    if not re.search(r"(?m)^Added by the image build: \d+", text):
        fail(f"{path} was written without os-packages.sh --base-db, so it does not mark the "
             f"packages the image build installs; bind the base image's package database")
    found = []
    # Package stanzas end where the appended license texts begin.
    for stanza in re.split(r"(?m)^={20,}$", text, maxsplit=1)[0].split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.split("\n") if ": " in line)
        if fields.get("Package") and fields.get("Requested") == "yes":
            found.append((fields["Package"], fields.get("License")))
    return found


def package_terms(args):
    declared = {}
    for item in args.package:
        name, _, expression = item.partition("=")
        if not name or not expression:
            fail(f"--package {item!r}: use NAME=EXPR or NAME=none", 2)
        declared[name] = expression
    needed, problems, seen = set(), [], set()
    for path in args.os_packages:
        for name, license in requested_packages(path):
            seen.add(name)
            expression = declared.get(name, license)
            if expression == "none":
                continue
            if not expression:
                problems.append(f"{name} (requested OS package): pass --package {name}=<SPDX expression>")
                continue
            if name not in declared:
                try:
                    parse(expression)
                except ValueError:
                    problems.append(f"{name}: declared license {expression!r} is not SPDX; "
                                    f"pass --package {name}=<SPDX expression> or {name}=none")
                    continue
            needed |= terms(expression, f"{path}: {name}")
    for name in sorted(set(declared) - seen):
        problems.append(f"--package {name}: not a requested package in {', '.join(args.os_packages) or '-'}")
    if problems:
        for message in problems:
            print(f"error: {message}", file=sys.stderr)
        fail("OS package licenses incomplete")
    return needed


def required(args):
    own = terms(args.own, "--own")
    needed = set()
    for path in args.notices:
        for name, expression in notice_licenses(path):
            needed |= terms(expression, f"{path}: {name}")
    needed |= package_terms(args)
    for term in args.add:
        needed |= terms(term, "--add")
    return own, needed - own


def render(own, rest):
    ordered = sorted(own) + sorted(rest)
    if len(ordered) == 1:
        return ordered[0][1:-1] if ordered[0].startswith("(") else ordered[0]
    return " AND ".join(ordered)


def strip_heredocs(text):
    """Drop heredoc bodies (RUN <<EOF ... EOF) so their lines are not read as instructions."""
    lines, out, ends = text.split("\n"), [], []
    for line in lines:
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        out.append(line)
        ends = re.findall(r"<<-?[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?", line)
    return "\n".join(out)


def dockerfile_label(path):
    text = strip_heredocs(Path(path).read_text(encoding="utf-8"))
    text = re.sub(r"\\\r?\n", " ", text)  # join continuation lines
    stages = re.split(r"(?im)^\s*FROM\s", text)
    final = stages[-1]
    key = re.escape(LABEL)
    values = re.findall(r"(?im)^\s*LABEL\s.*?" + key + r"""=("(?:[^"\\]|\\.)*"|\S+)""", final)
    # Legacy form: LABEL key value (one key, the rest of the line is the value).
    values += re.findall(r"(?im)^\s*LABEL\s+" + key + r"\s+([^=\n]+?)\s*$", final)
    if not values:
        fail(f"{path}: the final stage has no {LABEL} LABEL")
    value = values[-1].strip('"')
    if "$" in value:
        fail(f"{path}: {LABEL} uses a variable; write the expression literally")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("expr", "check"):
        command = sub.add_parser(mode)
        command.add_argument("--own", required=True, help="SPDX expression of the project's own code")
        command.add_argument("--add", action="append", default=[],
                             help="extra term for a component no notices file lists (repeatable)")
        command.add_argument("--os-packages", action="append", default=[],
                             help="OS-Packages.txt written with --base-db (repeatable)")
        command.add_argument("--package", action="append", default=[],
                             help="NAME=EXPR or NAME=none for a requested OS package (repeatable)")
        command.add_argument("notices", nargs="*", help="generated notices files")
        if mode == "check":
            command.add_argument("--label", required=True, help="label value of the built image")
    command = sub.add_parser("dockerfile")
    command.add_argument("path")
    args = parser.parse_args()

    if args.mode == "dockerfile":
        print(dockerfile_label(args.path))
        return
    own, rest = required(args)
    expected = render(own, rest)
    if args.mode == "expr":
        print(expected)
        return
    if not args.label.strip():
        fail(f"the image has no {LABEL} label; expected: {expected}")
    actual = terms(args.label, "--label")
    wanted = own | rest
    missing, extra = sorted(wanted - actual), sorted(actual - wanted)
    if missing or extra:
        for term in missing:
            print(f"missing from label: {term}", file=sys.stderr)
        for term in extra:
            print(f"not backed by any notices file or --add: {term}", file=sys.stderr)
        fail(f"{LABEL} does not match the image contents; expected:\n{expected}")
    print(f"ok: {LABEL} matches ({len(wanted)} terms)", file=sys.stderr)


if __name__ == "__main__":
    main()
