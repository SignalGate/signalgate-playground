#!/usr/bin/env python3
"""Fail when a change contains a term from a private deny-list.

Stdlib only: the CI job runs it with a bare ``python3`` and installs nothing.

The deny-list never lives in this repository. CI reads it from the
``DISCLOSURE_DENY_LIST`` environment variable, which the workflow fills from a
repository secret holding the list base64-encoded on one line (so the runner
masks it as one value). Locally, point ``--list`` at a plain-text file kept
outside the repo.

List format, one entry per line:

    # comment        ignored, like blank lines
    term             matched case-insensitively as a whole word: a letter or
                     digit right before or after it means no match
    re:PATTERN       a regular expression, matched case-insensitively
    allow:PHRASE     removed from the text before matching (a public phrase
                     that happens to contain a listed term)
    canary:TOKEN     required; the self-test must find it, which proves the
                     list arrived whole and the matcher works

What is scanned:

    --base B         the files the change B...--head adds or modifies, read
                     from --head, plus its commit messages
    --paths-file F   the files listed in F, read from --head as raw bytes; a
                     listed file that --head doesn't have (a deleted one) is
                     skipped. F ends each path with a NUL (so any file
                     name survives), or holds one path per line.
    neither          every tracked file on disk; with --git-dir, every file
                     of --head
    --text-file F    also the text of F (repeatable), e.g. a pull request's
                     title, body or commit messages
    --text-env NAME  also the text of an environment variable (repeatable)
    --messages-since R
                     also the messages of the commits a push brought: every
                     commit --head has that R doesn't. An R of all zeros, the
                     way a push that creates a branch reports it, means every
                     commit --head has. An R git can't find, or a shallow
                     repository that may be missing commits, fails closed.

``--git-dir`` sends every git command to that repository. With
``--paths-file`` the scanner reads git objects only, so it works on a commit
fetched into a scratch repository that has no checkout.

``--maintainer-paths`` takes comma-separated globs: ``*`` and ``?`` stay
inside one directory, ``**`` spans directories, anything else matches itself,
and case is ignored. When a path the change touches (deleted ones included)
matches one, the scanner prints ``FAIL (needs a maintainer)`` and reads no
file at all. Without the flag no path rule applies.

In CI the output is a single line: PASS, FAIL, or FAIL with a reason that
says nothing about the change. Which file, line or term matched, and how
many, never reaches the log. ``--explain`` prints those details for someone
who holds the list, and refuses to run in CI.

Exit status: 0 PASS; 1 FAIL, a listed term was found or the change needs a
maintainer; 2 FAIL, the check isn't configured (no usable list, or git could
not read the change) or its self-test failed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

LIST_ENV = "DISCLOSURE_DENY_LIST"
EXIT_PASS, EXIT_FOUND, EXIT_NOT_CONFIGURED = 0, 1, 2

NOT_CONFIGURED = "FAIL (the check isn't configured)"
SELF_TEST_FAILED = "FAIL (the check's self-test failed)"
NEEDS_MAINTAINER = "FAIL (needs a maintainer)"

_WORD = "a-z0-9"


class ListError(ValueError):
    """The deny-list is missing, empty or malformed."""


class GitError(RuntimeError):
    """git could not read what the scan needs. It carries no message, so
    nothing about the change can reach the log through it."""


@dataclass
class DenyList:
    terms: list[str] = field(default_factory=list)
    patterns: list[re.Pattern[str]] = field(default_factory=list)
    allows: list[re.Pattern[str]] = field(default_factory=list)
    canaries: list[str] = field(default_factory=list)
    _terms_re: re.Pattern[str] | None = None

    def matches(self, text: str) -> list[str]:
        """Every entry found in ``text``, after the allowed phrases are removed."""
        lowered = text.lower()
        for allowed in self.allows:
            lowered = allowed.sub(" ", lowered)
        found: list[str] = []
        if self._terms_re is not None:
            found.extend(m.group(0) for m in self._terms_re.finditer(lowered))
        found.extend(p.pattern for p in self.patterns if p.search(lowered))
        return found


def _term_regex(term: str) -> str:
    escaped = re.escape(term.lower())
    before = f"(?<![{_WORD}])" if term[0].isalnum() else ""
    after = f"(?![{_WORD}])" if term[-1].isalnum() else ""
    return f"{before}{escaped}{after}"


def decode_list(raw: str | None) -> str | None:
    """The list as text. The CI secret holds it base64-encoded on one line, so
    the runner can mask it as a single value in the log; a plain-text list, such
    as a local file, is used as it is."""
    if not raw:
        return raw
    try:
        decoded = base64.b64decode("".join(raw.split()), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return raw
    return decoded if "canary:" in decoded else raw


def parse_list(text: str | None) -> DenyList:
    """Build a ``DenyList``; raises ``ListError`` for anything unusable."""
    if not text or not text.strip():
        raise ListError("empty")
    deny = DenyList()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, _, value = line.partition(":")
        value = value.strip()
        if kind == "re" and value:
            try:
                deny.patterns.append(re.compile(value, re.IGNORECASE))
            except re.error as exc:
                raise ListError("bad pattern") from exc
        elif kind == "allow" and value:
            deny.allows.append(re.compile(re.escape(value.lower())))
        elif kind == "canary" and value:
            deny.canaries.append(value)
        else:
            deny.terms.append(line)
    if not deny.canaries:
        raise ListError("no canary")
    every_term = deny.terms + deny.canaries
    deny._terms_re = re.compile("|".join(_term_regex(t) for t in every_term))
    return deny


def self_test(deny: DenyList) -> bool:
    """True when every canary and every plain term is found in a probe."""
    probes = [f"probe {entry} probe" for entry in deny.canaries + deny.terms]
    return all(deny.matches(probe) for probe in probes)


@dataclass(frozen=True)
class Source:
    """One piece of text to scan: a file's contents, a commit message, a title.

    ``name`` is scanned too when ``scan_name`` is set (a file's path); a text
    file given on the command line is judged on its contents only, since its
    name is wherever the caller happened to write it.
    """

    name: str
    data: bytes
    scan_name: bool = True


@dataclass(frozen=True)
class Hit:
    source: str
    line: int
    entry: str


def scan(sources: Iterable[Source], deny: DenyList) -> list[Hit]:
    hits: list[Hit] = []
    for source in sources:
        # A listed term in a file's name counts as much as one in its text.
        header = source.name if source.scan_name else ""
        text = header + "\n" + source.data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines()):
            hits.extend(Hit(source.name, number, entry) for entry in deny.matches(line))
    return hits


# --- which paths a change touches ---------------------------------------------


def _glob_regex(glob: str) -> re.Pattern[str]:
    """``*`` and ``?`` stay inside one directory, ``**`` spans directories,
    every other character matches itself; case is ignored, which only ever
    makes the rule stricter."""
    parts: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            parts.append(".*")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def maintainer_globs(spec: str) -> list[re.Pattern[str]]:
    """The comma-separated globs of ``--maintainer-paths``."""
    return [_glob_regex(glob.strip()) for glob in spec.split(",") if glob.strip()]


def needs_maintainer(paths: Iterable[str], globs: list[re.Pattern[str]]) -> list[str]:
    """The paths that only a maintainer may change."""
    return [path for path in paths if any(glob.fullmatch(path) for glob in globs)]


def read_paths_file(name: str) -> list[str]:
    """The paths listed in a file: each ends with a NUL when the file holds
    one (so any file name survives), one per line otherwise."""
    with open(name, "rb") as handle:
        data = handle.read()
    records = data.split(b"\0") if b"\0" in data else data.splitlines()
    return [record.decode("utf-8", errors="replace") for record in records if record]


# --- reading through git ------------------------------------------------------


def _git(git_dir: str | None, *args: str) -> bytes:
    command = ["git"] if git_dir is None else ["git", "--git-dir", git_dir]
    try:
        return subprocess.run([*command, *args], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise GitError from None


def tree_sources() -> list[Source]:
    """Every tracked file, as it is on disk. A symlink is scanned as the path it
    points to, never followed, so nothing outside the repo is ever read."""
    sources = []
    for path in _git(None, "ls-files", "-z").decode("utf-8").split("\0"):
        if not path:
            continue
        if os.path.islink(path):
            sources.append(Source(path, os.readlink(path).encode("utf-8")))
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                sources.append(Source(path, handle.read()))
    return sources


def _tree_entries(git_dir: str | None, head: str) -> dict[str, list[tuple[str, str]]]:
    """Every entry of ``head``'s tree, by path: its object type and id."""
    entries: dict[str, list[tuple[str, str]]] = {}
    for record in _git(git_dir, "ls-tree", "-r", "-z", "--full-tree", head).split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        _mode, kind, object_id = meta.decode("ascii").split()
        # Names are compared as decoded text, the way a path list carries
        # them; two names that decode alike are both read.
        entries.setdefault(raw_path.decode("utf-8", errors="replace"), []).append((kind, object_id))
    return entries


def blob_sources(git_dir: str | None, head: str, paths: Iterable[str] | None) -> list[Source]:
    """The listed files (every file when ``paths`` is None) as ``head`` has
    them, read from git's objects as raw bytes. A symlink is its target path;
    a submodule is its name alone. A path ``head`` doesn't have is skipped."""
    entries = _tree_entries(git_dir, head)
    wanted = entries if paths is None else dict.fromkeys(paths)
    sources = []
    for path in wanted:
        for kind, object_id in entries.get(path, []):
            data = _git(git_dir, "cat-file", "blob", object_id) if kind == "blob" else b""
            sources.append(Source(path, data))
    return sources


def changed_paths(git_dir: str | None, base: str, head: str) -> list[str]:
    """Every path the change ``base...head`` touches, deleted ones included;
    a rename counts as both of its names."""
    names = _git(git_dir, "diff", "--name-only", "-z", "--no-renames", f"{base}...{head}")
    return [path for path in names.decode("utf-8", errors="replace").split("\0") if path]


def commit_messages(git_dir: str | None, base: str, head: str) -> Source:
    return Source("commit messages", _git(git_dir, "log", "--format=%B", f"{base}..{head}"))


# A push that creates a branch reports the commit before it as all zeros.
_NO_COMMIT = re.compile(r"0{40}|0{64}")


def pushed_messages(git_dir: str | None, before: str, head: str) -> Source:
    """The messages of the commits a push brought: those ``head`` has and
    ``before`` doesn't, which after a force push are the rewritten ones. A
    ``before`` of all zeros means every commit ``head`` has. A ``before`` that
    is empty, looks like an option or isn't a commit git can find is an error,
    never an empty range; so is a shallow repository, which may not hold every
    commit in between."""
    if not before or before.startswith("-"):
        raise GitError
    if _git(git_dir, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise GitError
    if _NO_COMMIT.fullmatch(before):
        revisions = [head]
    else:
        _git(git_dir, "rev-parse", "--verify", "--quiet", f"{before}^{{commit}}")
        revisions = [f"{before}..{head}"]
    return Source("commit messages", _git(git_dir, "log", "--format=%B", *revisions))


def _in_ci(env: Mapping[str, str]) -> bool:
    return bool(env.get("CI") or env.get("GITHUB_ACTIONS"))


def _revision(value: str) -> str:
    # A leading dash would reach git as an option.
    if not value or value.startswith("-"):
        raise argparse.ArgumentTypeError("not a revision")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan changes against a private deny-list.")
    parser.add_argument("--list", help=f"deny-list file (default: ${LIST_ENV})")
    parser.add_argument("--git-dir", metavar="PATH", help="run every git command against this")
    parser.add_argument("--base", type=_revision, help="scan only what changed since this commit")
    parser.add_argument("--head", type=_revision, default="HEAD", help="the change's last commit")
    parser.add_argument(
        "--paths-file",
        metavar="FILE",
        help="scan these files of --head (each ending in a NUL, or one per line)",
    )
    parser.add_argument(
        "--maintainer-paths",
        metavar="GLOBS",
        help="comma-separated globs; a change touching one needs a maintainer",
    )
    parser.add_argument(
        "--text-file",
        action="append",
        default=[],
        metavar="FILE",
        help="also scan the text in this file (repeatable)",
    )
    parser.add_argument(
        "--text-env",
        action="append",
        default=[],
        metavar="NAME",
        help="also scan the text in this environment variable (repeatable)",
    )
    parser.add_argument(
        "--messages-since",
        metavar="REV",
        help="also scan the messages of the commits --head has that REV doesn't "
        "(all zeros: every commit of --head)",
    )
    parser.add_argument("--explain", action="store_true", help="show what matched (never in CI)")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = _parser()
    args = parser.parse_args(argv)
    if args.base and args.paths_file is not None:
        parser.error("--base and --paths-file don't go together")
    globs = None
    if args.maintainer_paths is not None:
        if not args.base and args.paths_file is None:
            parser.error("--maintainer-paths needs --base or --paths-file")
        globs = maintainer_globs(args.maintainer_paths)
        if not globs:
            parser.error("--maintainer-paths needs at least one glob")

    if args.explain and _in_ci(env):
        print("FAIL (--explain is for local use only)")
        return EXIT_NOT_CONFIGURED

    try:
        if args.list:
            with open(args.list, encoding="utf-8") as handle:
                raw = handle.read()
        else:
            raw = env.get(LIST_ENV)
        deny = parse_list(decode_list(raw))
    except (OSError, ListError):
        print(NOT_CONFIGURED)
        return EXIT_NOT_CONFIGURED
    if not self_test(deny):
        print(SELF_TEST_FAILED)
        return EXIT_NOT_CONFIGURED

    try:
        if args.paths_file is not None:
            paths = read_paths_file(args.paths_file)
        elif args.base:
            paths = changed_paths(args.git_dir, args.base, args.head)
        else:
            paths = None

        # Decided before any file is read: such a change is never scanned here.
        flagged = needs_maintainer(paths, globs) if paths is not None and globs else []
        if flagged:
            if args.explain:
                for path in flagged:
                    print(f"{path}: needs a maintainer")
            print(NEEDS_MAINTAINER)
            return EXIT_FOUND

        if paths is None and args.git_dir is None:
            sources = tree_sources()
        else:
            sources = blob_sources(args.git_dir, args.head, paths)
            if args.base:
                sources.append(commit_messages(args.git_dir, args.base, args.head))
        # Checked for None, not truth: an empty value fails closed, never
        # quietly scans no messages.
        if args.messages_since is not None:
            sources.append(pushed_messages(args.git_dir, args.messages_since, args.head))
        for name in args.text_file:
            with open(name, "rb") as handle:
                sources.append(Source(name, handle.read(), scan_name=False))
    except (GitError, OSError):
        print(NOT_CONFIGURED)
        return EXIT_NOT_CONFIGURED
    sources += [Source(name, env.get(name, "").encode("utf-8")) for name in args.text_env]
    hits = scan(sources, deny)

    if args.explain:
        for hit in hits:
            print(f"{hit.source}:{hit.line}: {hit.entry}")
    print("FAIL" if hits else "PASS")
    return EXIT_FOUND if hits else EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
