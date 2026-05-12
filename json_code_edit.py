#!/usr/bin/env python3
"""Convert git patches to searchable JSON edits, then apply those edits.

The JSON format produced by this script is intentionally text-based instead of
line-number-based. Edits search for exact text and use nearby context only to
disambiguate repeated matches.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
HASH_ALGORITHM = "sha256"


class EditError(RuntimeError):
    """Raised when an edit cannot be applied safely."""


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as source:
        return source.read()


@dataclass
class PatchLine:
    kind: str
    text: str


@dataclass
class PatchFile:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list[list[PatchLine]] = field(default_factory=list)
    new_file_header: bool = False
    deleted_file_header: bool = False
    binary_patch: bool = False

    @property
    def is_new_file(self) -> bool:
        return self.old_path is None or self.new_file_header

    @property
    def is_deleted_file(self) -> bool:
        return self.new_path is None or self.deleted_file_header


@dataclass
class FileApplyState:
    mode: str
    old_sha256: str | None
    new_sha256: str | None
    reported_already: bool = False


def _strip_git_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _decode_git_quoted_path(path: str) -> str:
    if not (path.startswith('"') and path.endswith('"')):
        return path

    decoded: list[str] = []
    index = 1
    end = len(path) - 1
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
    }
    while index < end:
        char = path[index]
        if char != "\\":
            decoded.append(char)
            index += 1
            continue

        index += 1
        if index >= end:
            decoded.append("\\")
            break

        escaped = path[index]
        if escaped in escapes:
            decoded.append(escapes[escaped])
            index += 1
            continue

        if "0" <= escaped <= "7":
            octal = escaped
            index += 1
            while index < end and len(octal) < 3 and "0" <= path[index] <= "7":
                octal += path[index]
                index += 1
            decoded.append(chr(int(octal, 8)))
            continue

        decoded.append(escaped)
        index += 1

    return "".join(decoded)


def _read_quoted_token(payload: str) -> tuple[str, str] | None:
    if not payload.startswith('"'):
        return None

    escaped = False
    for index in range(1, len(payload)):
        char = payload[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return payload[: index + 1], payload[index + 1 :].lstrip()
    return None


def _split_git_path_tokens(payload: str) -> list[str]:
    payload = payload.strip()
    tokens: list[str] = []
    while payload and len(tokens) < 2:
        quoted = _read_quoted_token(payload)
        if quoted is not None:
            token, payload = quoted
            tokens.append(_decode_git_quoted_path(token))
            continue

        if payload.startswith("a/") and " b/" in payload:
            separator_index = payload.find(" b/")
            tokens.append(payload[:separator_index])
            tokens.append(payload[separator_index + 1 :])
            break

        parts = payload.split(None, 1)
        tokens.append(parts[0])
        payload = parts[1] if len(parts) == 2 else ""

    return tokens


def _first_path_token(payload: str) -> str:
    payload = payload.rstrip("\r\n")
    quoted = _read_quoted_token(payload)
    if quoted is not None:
        token, _rest = quoted
        return _decode_git_quoted_path(token)
    if "\t" in payload:
        payload = payload.split("\t", 1)[0]
    if payload.startswith(("a/", "b/")):
        return payload
    tokens = payload.split()
    return tokens[0] if tokens else ""


def _parse_unified_path(payload: str) -> str | None:
    path = _first_path_token(payload)
    if path == "/dev/null":
        return None
    return _strip_git_prefix(path)


def _parse_git_paths(line: str) -> tuple[str | None, str | None]:
    payload = line[len("diff --git ") :].strip()
    tokens = _split_git_path_tokens(payload)
    if len(tokens) < 2:
        return None, None
    return _strip_git_prefix(tokens[0]), _strip_git_prefix(tokens[1])


def _is_hunk_header(line: str) -> bool:
    return re.match(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", line) is not None


def _validate_hunk_header(line: str) -> None:
    if not _is_hunk_header(line):
        raise EditError(f"invalid hunk header: {line.rstrip()}")


def parse_unified_diff(patch_text: str) -> list[PatchFile]:
    """Parse a unified/git diff without trusting hunk line numbers or counts.

    AI-generated patches often preserve the textual hunk body but get the
    numbers in the ``@@ -old,count +new,count @@`` header wrong.  The safe
    information is the hunk body itself: every hunk content line must start
    with a space, ``+``, ``-``, or the special no-newline marker.  We therefore
    use hunk headers only as separators and collect body lines until the next
    hunk or file boundary.
    """
    files: list[PatchFile] = []
    current: PatchFile | None = None
    current_hunk: list[PatchLine] | None = None
    last_hunk: list[PatchLine] | None = None

    def ensure_file() -> PatchFile:
        nonlocal current
        if current is None:
            current = PatchFile()
        return current

    for raw_line in patch_text.splitlines(keepends=True):
        if raw_line.startswith("\\ No newline at end of file"):
            hunk = current_hunk or last_hunk
            if hunk and hunk[-1].text.endswith("\n"):
                hunk[-1].text = hunk[-1].text[:-1]
            continue

        if raw_line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            old_path, new_path = _parse_git_paths(raw_line)
            current = PatchFile(old_path=old_path, new_path=new_path)
            current_hunk = None
            last_hunk = None
            continue

        if raw_line.startswith("@@ "):
            _validate_hunk_header(raw_line)
            current_hunk = []
            last_hunk = current_hunk
            ensure_file().hunks.append(current_hunk)
            continue

        if current_hunk is not None:
            if raw_line and raw_line[0] in {" ", "+", "-"}:
                current_hunk.append(PatchLine(kind=raw_line[0], text=raw_line[1:]))
                continue

            # A non-hunk line means the hunk body ended. Continue parsing the
            # line below as ordinary file metadata. This keeps malformed hunk
            # counts from making conversion fail, while still preserving lines
            # that begin with +, -, or a space as hunk content.
            current_hunk = None

        if raw_line.startswith("--- "):
            ensure_file().old_path = _parse_unified_path(raw_line[4:])
            last_hunk = None
            continue

        if raw_line.startswith("+++ "):
            ensure_file().new_path = _parse_unified_path(raw_line[4:])
            last_hunk = None
            continue

        if raw_line.startswith("new file mode"):
            ensure_file().new_file_header = True
            last_hunk = None
            continue

        if raw_line.startswith("deleted file mode"):
            ensure_file().deleted_file_header = True
            last_hunk = None
            continue

        if raw_line.startswith(("GIT binary patch", "Binary files ")):
            ensure_file().binary_patch = True
            last_hunk = None
            continue

        last_hunk = None

    if current is not None:
        files.append(current)

    return [patch_file for patch_file in files if patch_file.hunks or patch_file.old_path or patch_file.new_path]


def _context_before(lines: list[PatchLine], start: int, anchor_lines: int) -> str:
    context: list[str] = []
    index = start - 1
    while index >= 0 and lines[index].kind == " " and len(context) < anchor_lines:
        context.append(lines[index].text)
        index -= 1
    return "".join(reversed(context))


def _context_after(lines: list[PatchLine], end: int, anchor_lines: int) -> str:
    context: list[str] = []
    index = end
    while index < len(lines) and lines[index].kind == " " and len(context) < anchor_lines:
        context.append(lines[index].text)
        index += 1
    return "".join(context)


def _hunk_to_edits(hunk: list[PatchLine], path: str, anchor_lines: int) -> list[dict[str, Any]]:
    edits: list[dict[str, Any]] = []
    index = 0
    while index < len(hunk):
        while index < len(hunk) and hunk[index].kind == " ":
            index += 1
        if index >= len(hunk):
            break

        start = index
        while index < len(hunk) and hunk[index].kind != " ":
            index += 1
        end = index

        old = "".join(line.text for line in hunk[start:end] if line.kind == "-")
        new = "".join(line.text for line in hunk[start:end] if line.kind == "+")
        if old == new:
            continue

        before = _context_before(hunk, start, anchor_lines)
        after = _context_after(hunk, end, anchor_lines)
        context = {"before": before, "after": after}

        if old and new:
            edits.append({"op": "replace", "path": path, "old": old, "new": new, "context": context})
        elif old:
            edits.append({"op": "delete", "path": path, "old": old, "context": context})
        elif new:
            edits.append({"op": "insert", "path": path, "text": new, "where": {"after": before, "before": after}})

    return edits


def _new_file_content(patch_file: PatchFile) -> str:
    return "".join(line.text for hunk in patch_file.hunks for line in hunk if line.kind in {"+", " "})


def _old_file_content(patch_file: PatchFile) -> str:
    return "".join(line.text for hunk in patch_file.hunks for line in hunk if line.kind in {"-", " "})


def _line_context_before(lines: list[str], start: int, anchor_lines: int) -> str:
    if anchor_lines == 0:
        return ""
    return "".join(lines[max(0, start - anchor_lines) : start])


def _line_context_after(lines: list[str], end: int, anchor_lines: int) -> str:
    if anchor_lines == 0:
        return ""
    return "".join(lines[end : end + anchor_lines])


def _diff_text_to_edits(old_text: str, new_text: str, path: str, anchor_lines: int) -> list[dict[str, Any]]:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    groups: list[tuple[int, int, int, int]] = []
    current: list[int] | None = None
    pending_equal: tuple[str, int, int, int, int] | None = None

    for opcode in matcher.get_opcodes():
        tag, old_start, old_end, new_start, new_end = opcode
        if tag == "equal":
            if current is not None:
                pending_equal = opcode
            continue

        if current is None:
            current = [old_start, old_end, new_start, new_end]
            pending_equal = None
            continue

        equal_lines = 0 if pending_equal is None else pending_equal[2] - pending_equal[1]
        if equal_lines <= anchor_lines:
            current[1] = old_end
            current[3] = new_end
        else:
            groups.append((current[0], current[1], current[2], current[3]))
            current = [old_start, old_end, new_start, new_end]
        pending_equal = None

    if current is not None:
        groups.append((current[0], current[1], current[2], current[3]))

    edits: list[dict[str, Any]] = []
    for old_start, old_end, new_start, new_end in groups:
        old = "".join(old_lines[old_start:old_end])
        new = "".join(new_lines[new_start:new_end])
        if old == new:
            continue

        before = _line_context_before(old_lines, old_start, anchor_lines)
        after = _line_context_after(old_lines, old_end, anchor_lines)

        if old and new:
            edits.append({"op": "replace", "path": path, "old": old, "new": new, "context": {"before": before, "after": after}})
        elif old:
            edits.append({"op": "delete", "path": path, "old": old, "context": {"before": before, "after": after}})
        elif new:
            edits.append({"op": "insert", "path": path, "text": new, "where": {"after": before, "before": after}})

    return edits


def _iter_relative_files(root: Path) -> set[str]:
    files: set[str] = set()
    if not root.exists():
        raise EditError(f"directory does not exist: {root}")
    if not root.is_dir():
        raise EditError(f"not a directory: {root}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        files.add(relative.as_posix())
    return files


def _append_file_change(
    edits: list[dict[str, Any]],
    files: list[dict[str, Any]],
    path: str,
    old_text: str | None,
    new_text: str | None,
    anchor_lines: int,
) -> None:
    if old_text == new_text:
        return

    files.append(
        {
            "path": path,
            "old_sha256": _sha256_text(old_text) if old_text is not None else None,
            "new_sha256": _sha256_text(new_text) if new_text is not None else None,
        }
    )

    if old_text is None:
        if new_text is not None:
            edits.append({"op": "create_file", "path": path, "content": new_text})
        return

    if new_text is None:
        edits.append({"op": "delete_file", "path": path, "expected_content": old_text})
        return

    edits.extend(_diff_text_to_edits(old_text, new_text, path, anchor_lines))


def build_script_from_dirs(old_dir: Path | str, new_dir: Path | str, anchor_lines: int = 3) -> dict[str, Any]:
    """Build a JSON edit script from two real directory snapshots."""
    if anchor_lines < 0:
        raise ValueError("anchor_lines must be non-negative")

    old_root = Path(old_dir)
    new_root = Path(new_dir)
    paths = sorted(_iter_relative_files(old_root) | _iter_relative_files(new_root))
    edits: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for path in paths:
        old_path = old_root / path
        new_path = new_root / path
        old_text = _read_text_preserve_newlines(old_path) if old_path.exists() else None
        new_text = _read_text_preserve_newlines(new_path) if new_path.exists() else None
        _append_file_change(edits, files, path, old_text, new_text, anchor_lines)

    return {"version": SCHEMA_VERSION, "hash_algorithm": HASH_ALGORITHM, "files": files, "edits": edits}


def _run_git(repo_dir: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        command = "git " + " ".join(args)
        raise EditError(f"{command} failed: {stderr}")
    return completed


def _decode_git_paths(output: bytes) -> set[str]:
    return {chunk.decode("utf-8", "surrogateescape") for chunk in output.split(b"\0") if chunk}


def _decode_text_blob(blob: bytes, path: str) -> str:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"{path}: only UTF-8 text files are supported") from exc


def _git_blob_exists(repo_dir: Path, rev: str, path: str) -> bool:
    completed = _run_git(repo_dir, ["cat-file", "-e", f"{rev}:{path}"], check=False)
    return completed.returncode == 0


def _git_show_text(repo_dir: Path, rev: str, path: str) -> str | None:
    if not _git_blob_exists(repo_dir, rev, path):
        return None
    completed = _run_git(repo_dir, ["show", f"{rev}:{path}"])
    return _decode_text_blob(completed.stdout, path)


def build_script_from_git(repo_dir: Path | str = ".", base: str = "HEAD", anchor_lines: int = 3) -> dict[str, Any]:
    """Build a JSON edit script from real changes in a Git worktree."""
    if anchor_lines < 0:
        raise ValueError("anchor_lines must be non-negative")

    repo = Path(repo_dir)
    root_text = _run_git(repo, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8", "replace").strip()
    root = Path(root_text)
    base_commit = _run_git(root, ["rev-parse", "--verify", base]).stdout.decode("utf-8", "replace").strip()

    diff_paths = _decode_git_paths(_run_git(root, ["diff", "--name-only", "-z", base_commit]).stdout)
    untracked_paths = _decode_git_paths(_run_git(root, ["ls-files", "--others", "--exclude-standard", "-z"]).stdout)
    paths = sorted(diff_paths | untracked_paths)

    edits: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for path in paths:
        old_text = _git_show_text(root, base_commit, path)
        worktree_path = root / path
        if worktree_path.exists():
            if not worktree_path.is_file():
                raise EditError(f"{path}: only regular text files are supported")
            new_text = _read_text_preserve_newlines(worktree_path)
        else:
            new_text = None
        before_count = len(edits)
        _append_file_change(edits, files, path, old_text, new_text, anchor_lines)
        if path in diff_paths and old_text == new_text and len(edits) == before_count:
            raise EditError(f"{path}: content is unchanged; mode-only or unsupported metadata changes are not supported")

    return {
        "version": SCHEMA_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "base_commit": base_commit,
        "files": files,
        "edits": edits,
    }


def convert_patch_to_json(patch_text: str, anchor_lines: int = 3) -> dict[str, Any]:
    if anchor_lines < 0:
        raise ValueError("anchor_lines must be non-negative")

    edits: list[dict[str, Any]] = []
    for patch_file in parse_unified_diff(patch_text):
        if patch_file.binary_patch:
            path = patch_file.new_path or patch_file.old_path or "<unknown>"
            raise EditError(f"{path}: binary patches are not supported")

        if patch_file.is_new_file:
            if patch_file.new_path is None:
                raise EditError("new file patch is missing a destination path")
            edits.append({"op": "create_file", "path": patch_file.new_path, "content": _new_file_content(patch_file)})
            continue

        if patch_file.is_deleted_file:
            if patch_file.old_path is None:
                raise EditError("deleted file patch is missing a source path")
            edits.append({"op": "delete_file", "path": patch_file.old_path, "expected_content": _old_file_content(patch_file)})
            continue

        if patch_file.old_path is None or patch_file.new_path is None:
            raise EditError("modified file patch is missing a path")

        if patch_file.old_path != patch_file.new_path:
            edits.append({"op": "move_file", "from": patch_file.old_path, "path": patch_file.new_path})

        for hunk in patch_file.hunks:
            edits.extend(_hunk_to_edits(hunk, patch_file.new_path, anchor_lines))

    return {"version": SCHEMA_VERSION, "edits": edits}


def _all_occurrences(haystack: str, needle: str) -> list[int]:
    if needle == "":
        return []
    positions: list[int] = []
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index == -1:
            return positions
        positions.append(index)
        start = index + len(needle)


def _context_matches(content: str, start: int, end: int, before: str, after: str) -> bool:
    if before and not content[:start].endswith(before):
        return False
    if after and not content[end:].startswith(after):
        return False
    return True


def _dominant_newline(content: str) -> str:
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _adapt_newlines(text: str, content: str) -> str:
    """Convert patch text to the target file's dominant newline style."""
    if not text or "\n" not in text:
        return text
    newline = _dominant_newline(content)
    normalized = text.replace("\r\n", "\n")
    return normalized.replace("\n", newline)


def _adapt_context(context: dict[str, Any] | None, content: str) -> dict[str, Any] | None:
    if context is None:
        return None
    return {
        "before": _adapt_newlines(str(context.get("before", "")), content),
        "after": _adapt_newlines(str(context.get("after", "")), content),
    }


def _adapt_where(where: dict[str, Any], content: str) -> dict[str, Any]:
    return {
        "after": _adapt_newlines(str(where.get("after", "")), content),
        "before": _adapt_newlines(str(where.get("before", "")), content),
    }


def _find_unique_span(
    content: str,
    needle: str,
    context: dict[str, Any] | None,
    path: Path,
    strict_context: bool,
) -> tuple[int, int]:
    positions = _all_occurrences(content, needle)
    if not positions:
        raise EditError(f"{path}: target text was not found")

    before = str((context or {}).get("before", ""))
    after = str((context or {}).get("after", ""))
    contextual = [
        position
        for position in positions
        if _context_matches(content, position, position + len(needle), before, after)
    ]

    if len(contextual) == 1:
        position = contextual[0]
        return position, position + len(needle)

    if len(contextual) > 1:
        raise EditError(f"{path}: target text is ambiguous even with context ({len(contextual)} matches)")

    if not strict_context and len(positions) == 1:
        position = positions[0]
        return position, position + len(needle)

    if strict_context:
        raise EditError(f"{path}: target text was found, but not with the required context")

    raise EditError(f"{path}: target text is ambiguous without matching context ({len(positions)} matches)")


def _already_has_replace(content: str, new: str, context: dict[str, Any] | None) -> bool:
    positions = _all_occurrences(content, new)
    if not positions:
        return False

    before = str((context or {}).get("before", ""))
    after = str((context or {}).get("after", ""))
    contextual = [
        position
        for position in positions
        if _context_matches(content, position, position + len(new), before, after)
    ]
    if contextual:
        return len(contextual) == 1
    return not before and not after and len(positions) == 1


def _already_has_delete(content: str, context: dict[str, Any] | None) -> bool:
    before = str((context or {}).get("before", ""))
    after = str((context or {}).get("after", ""))
    if before and after:
        return len(_all_occurrences(content, before + after)) == 1
    return False


def _already_has_insert(content: str, text: str, where: dict[str, Any]) -> bool:
    after_anchor = str(where.get("after", ""))
    before_anchor = str(where.get("before", ""))

    if after_anchor and before_anchor:
        return len(_all_occurrences(content, after_anchor + text + before_anchor)) == 1

    if after_anchor:
        matches = 0
        for after_start in _all_occurrences(content, after_anchor):
            position = after_start + len(after_anchor)
            if content[position : position + len(text)] == text:
                matches += 1
        return matches == 1

    if before_anchor:
        matches = 0
        for before_start in _all_occurrences(content, before_anchor):
            position = before_start - len(text)
            if position >= 0 and content[position:before_start] == text:
                matches += 1
        return matches == 1

    return False


def _find_insert_position(
    content: str,
    where: dict[str, Any],
    path: Path,
) -> int:
    after_anchor = str(where.get("after", ""))
    before_anchor = str(where.get("before", ""))

    if after_anchor and before_anchor:
        positions: list[int] = []
        for after_start in _all_occurrences(content, after_anchor):
            position = after_start + len(after_anchor)
            if content[position:].startswith(before_anchor):
                positions.append(position)
        if len(positions) == 1:
            return positions[0]
        if len(positions) > 1:
            raise EditError(f"{path}: insert location is ambiguous ({len(positions)} matches)")
        raise EditError(f"{path}: insert anchors were not found together")

    if after_anchor:
        positions = _all_occurrences(content, after_anchor)
        if len(positions) == 1:
            return positions[0] + len(after_anchor)
        if len(positions) > 1:
            raise EditError(f"{path}: after-anchor is ambiguous ({len(positions)} matches)")
        raise EditError(f"{path}: after-anchor was not found")

    if before_anchor:
        positions = _all_occurrences(content, before_anchor)
        if len(positions) == 1:
            return positions[0]
        if len(positions) > 1:
            raise EditError(f"{path}: before-anchor is ambiguous ({len(positions)} matches)")
        raise EditError(f"{path}: before-anchor was not found")

    raise EditError(f"{path}: insert edit has no anchors")


class FileStore:
    def __init__(self, base_dir: Path, dry_run: bool, unsafe_paths: bool) -> None:
        self.base_dir = base_dir.resolve()
        self.dry_run = dry_run
        self.unsafe_paths = unsafe_paths
        self.cache: dict[Path, str | None] = {}
        self.touched: set[Path] = set()

    def resolve(self, user_path: str) -> Path:
        path = Path(user_path)
        if path.is_absolute() and not self.unsafe_paths:
            raise EditError(f"absolute paths are disabled: {user_path}")
        resolved = (path if path.is_absolute() else self.base_dir / path).resolve()
        if not self.unsafe_paths:
            try:
                resolved.relative_to(self.base_dir)
            except ValueError as exc:
                raise EditError(f"path escapes the base directory: {user_path}") from exc
        return resolved

    def exists(self, path: Path) -> bool:
        if path in self.cache:
            return self.cache[path] is not None
        return path.exists()

    def read(self, path: Path) -> str:
        if path in self.cache:
            content = self.cache[path]
            if content is None:
                raise EditError(f"{path}: file has been deleted by an earlier edit")
            return content
        if not path.exists():
            raise EditError(f"{path}: file does not exist")
        content = _read_text_preserve_newlines(path)
        self.cache[path] = content
        return content

    def hash(self, path: Path) -> str:
        return _sha256_text(self.read(path))

    def write(self, path: Path, content: str) -> None:
        self.cache[path] = content
        self.touched.add(path)

    def delete(self, path: Path) -> None:
        self.cache[path] = None
        self.touched.add(path)

    def move(self, source: Path, destination: Path) -> None:
        if self.exists(destination):
            raise EditError(f"{destination}: destination already exists")
        self.write(destination, self.read(source))
        self.delete(source)

    def flush(self) -> None:
        if self.dry_run:
            return

        for path in sorted(self.touched):
            content = self.cache[path]
            if content is None:
                if path.exists():
                    path.unlink()
                continue

            path.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as temp_file:
                    temp_file.write(content)
                os.chmod(temp_name, mode)
                os.replace(temp_name, path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise


def _require_string(edit: dict[str, Any], field_name: str) -> str:
    value = edit.get(field_name)
    if not isinstance(value, str):
        raise EditError(f"edit is missing string field {field_name!r}: {edit!r}")
    return value


def _optional_hash(value: Any, field_name: str, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise EditError(f"file metadata for {path!r} has invalid {field_name}")
    return value


def _metadata_by_path(script: dict[str, Any], store: FileStore) -> dict[Path, FileApplyState]:
    raw_files = script.get("files", [])
    if raw_files is None:
        return {}
    if not isinstance(raw_files, list):
        raise EditError("script field 'files' must be a list when present")

    states: dict[Path, FileApplyState] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise EditError("script file metadata entries must be objects")
        user_path = raw_file.get("path")
        if not isinstance(user_path, str):
            raise EditError(f"file metadata entry is missing string field 'path': {raw_file!r}")
        path = store.resolve(user_path)
        if path in states:
            raise EditError(f"duplicate file metadata for {user_path!r}")

        old_sha256 = _optional_hash(raw_file.get("old_sha256"), "old_sha256", user_path)
        new_sha256 = _optional_hash(raw_file.get("new_sha256"), "new_sha256", user_path)

        if store.exists(path):
            current_sha256 = store.hash(path)
            if new_sha256 is not None and current_sha256 == new_sha256:
                mode = "already"
            elif old_sha256 is not None and current_sha256 == old_sha256:
                mode = "exact"
            else:
                mode = "drift"
        else:
            if new_sha256 is None:
                mode = "already"
            elif old_sha256 is None:
                mode = "exact"
            else:
                mode = "missing"

        states[path] = FileApplyState(mode=mode, old_sha256=old_sha256, new_sha256=new_sha256)

    return states


def _state_suffix(state: FileApplyState | None) -> str:
    if state is not None and state.mode == "drift":
        return " in drift mode"
    return ""


def _should_skip_for_already_applied(path: Path, state: FileApplyState | None, results: list[str]) -> bool:
    if state is None or state.mode != "already":
        return False
    if not state.reported_already:
        results.append(f"{path}: already up to date (whole file hash matched)")
        state.reported_already = True
    return True


def _verify_file_states(states: dict[Path, FileApplyState], store: FileStore, results: list[str]) -> None:
    for path, state in states.items():
        if state.mode in {"already", "drift"}:
            continue
        if state.mode == "missing":
            raise EditError(f"{path}: file is missing and cannot be drift-applied")

        if state.new_sha256 is None:
            if store.exists(path):
                raise EditError(f"{path}: expected file to be deleted, but it still exists")
            results.append(f"{path}: verified deleted")
            continue

        if not store.exists(path):
            raise EditError(f"{path}: expected final file to exist")

        final_sha256 = store.hash(path)
        if final_sha256 != state.new_sha256:
            raise EditError(f"{path}: final hash verification failed")
        results.append(f"{path}: verified final hash")


def apply_edit_script(
    script: dict[str, Any],
    base_dir: Path | str = ".",
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    unsafe_paths: bool = False,
    strict_context: bool = False,
) -> list[str]:
    if script.get("version") != SCHEMA_VERSION:
        raise EditError(f"unsupported script version: {script.get('version')!r}")
    if script.get("hash_algorithm", HASH_ALGORITHM) != HASH_ALGORITHM:
        raise EditError(f"unsupported hash algorithm: {script.get('hash_algorithm')!r}")
    edits = script.get("edits")
    if not isinstance(edits, list):
        raise EditError("script must contain an edits list")

    store = FileStore(Path(base_dir), dry_run=dry_run, unsafe_paths=unsafe_paths)
    results: list[str] = []
    file_states = _metadata_by_path(script, store)

    for index, raw_edit in enumerate(edits, start=1):
        if not isinstance(raw_edit, dict):
            raise EditError(f"edit #{index} must be an object")

        op = raw_edit.get("op")
        path = store.resolve(_require_string(raw_edit, "path"))
        state = file_states.get(path)
        if _should_skip_for_already_applied(path, state, results):
            continue
        if state is not None and state.mode == "missing":
            raise EditError(f"{path}: file is missing and cannot be drift-applied")

        if op == "create_file":
            content = _require_string(raw_edit, "content")
            if store.exists(path):
                existing = store.read(path)
                if existing == content:
                    results.append(f"{path}: already up to date")
                    continue
                if not overwrite:
                    raise EditError(f"{path}: file already exists")
            store.write(path, content)
            results.append(f"{path}: created{_state_suffix(state)}")

        elif op == "delete_file":
            expected = raw_edit.get("expected_content")
            content = store.read(path)
            if expected is not None and content != expected:
                raise EditError(f"{path}: file content does not match expected delete content")
            store.delete(path)
            results.append(f"{path}: deleted{_state_suffix(state)}")

        elif op == "move_file":
            source = store.resolve(_require_string(raw_edit, "from"))
            store.move(source, path)
            results.append(f"{source}: moved to {path}{_state_suffix(state)}")

        elif op in {"replace", "delete"}:
            old = _require_string(raw_edit, "old")
            new = _require_string(raw_edit, "new") if op == "replace" else ""
            content = store.read(path)
            context = raw_edit.get("context") if isinstance(raw_edit.get("context"), dict) else None
            old = _adapt_newlines(old, content)
            new = _adapt_newlines(new, content)
            context = _adapt_context(context, content)
            try:
                start, end = _find_unique_span(content, old, context, path, strict_context)
            except EditError:
                if op == "replace" and _already_has_replace(content, new, context):
                    results.append(f"{path}: already up to date")
                    continue
                if op == "delete" and _already_has_delete(content, context):
                    results.append(f"{path}: already up to date")
                    continue
                raise
            store.write(path, content[:start] + new + content[end:])
            results.append(f"{path}: {op} applied{_state_suffix(state)}")

        elif op == "insert":
            text = _require_string(raw_edit, "text")
            where = raw_edit.get("where")
            if not isinstance(where, dict):
                raise EditError(f"{path}: insert edit is missing a where object")
            content = store.read(path)
            text = _adapt_newlines(text, content)
            where = _adapt_where(where, content)
            if _already_has_insert(content, text, where):
                results.append(f"{path}: already up to date")
                continue
            position = _find_insert_position(content, where, path)
            store.write(path, content[:position] + text + content[position:])
            results.append(f"{path}: insert applied{_state_suffix(state)}")

        else:
            raise EditError(f"edit #{index} has unsupported op: {op!r}")

    _verify_file_states(file_states, store, results)
    store.flush()
    return results


def _read_argument_text(path_arg: str) -> str:
    if path_arg == "-":
        return sys.stdin.read()
    return Path(path_arg).read_text(encoding="utf-8")


def _write_output(path_arg: str | None, text: str) -> None:
    if path_arg:
        Path(path_arg).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert and apply searchable JSON code edits.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    to_json = subparsers.add_parser("to-json", help="convert a unified/git patch to JSON edits")
    to_json.add_argument("patch", help="patch file to read, or '-' for stdin")
    to_json.add_argument("-o", "--output", help="where to write JSON; stdout when omitted")
    to_json.add_argument("--anchor-lines", type=int, default=3, help="context lines to store around each edit")

    from_dirs = subparsers.add_parser("from-dirs", help="build JSON edits from before/after directory snapshots")
    from_dirs.add_argument("old_dir", help="directory containing the original files")
    from_dirs.add_argument("new_dir", help="directory containing the edited files")
    from_dirs.add_argument("-o", "--output", help="where to write JSON; stdout when omitted")
    from_dirs.add_argument("--anchor-lines", type=int, default=3, help="context lines to store around each edit")

    from_git = subparsers.add_parser("from-git", help="derive JSON edits from actual Git worktree changes")
    from_git.add_argument("--repo", default=".", help="Git worktree to inspect")
    from_git.add_argument("--base", default="HEAD", help="base revision to compare against")
    from_git.add_argument("-o", "--output", help="where to write JSON; stdout when omitted")
    from_git.add_argument("--anchor-lines", type=int, default=3, help="context lines to store around each edit")

    apply = subparsers.add_parser("apply", help="apply JSON edits to files")
    apply.add_argument("script", help="JSON edit script to apply")
    apply.add_argument("--base-dir", default=".", help="directory that edit paths are resolved under")
    apply.add_argument("--dry-run", action="store_true", help="validate and simulate without writing files")
    apply.add_argument("--overwrite", action="store_true", help="allow create_file to replace existing files")
    apply.add_argument("--unsafe-paths", action="store_true", help="allow absolute paths and paths outside base-dir")
    apply.add_argument("--strict-context", action="store_true", help="require stored context to match")
    apply.add_argument("--quiet", action="store_true", help="suppress per-edit output")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "to-json":
            script = convert_patch_to_json(_read_argument_text(args.patch), anchor_lines=args.anchor_lines)
            _write_output(args.output, json.dumps(script, indent=2) + "\n")
            return 0

        if args.command == "from-dirs":
            script = build_script_from_dirs(args.old_dir, args.new_dir, anchor_lines=args.anchor_lines)
            _write_output(args.output, json.dumps(script, indent=2) + "\n")
            return 0

        if args.command == "from-git":
            script = build_script_from_git(args.repo, base=args.base, anchor_lines=args.anchor_lines)
            _write_output(args.output, json.dumps(script, indent=2) + "\n")
            return 0

        if args.command == "apply":
            script = json.loads(_read_argument_text(args.script))
            results = apply_edit_script(
                script,
                args.base_dir,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
                unsafe_paths=args.unsafe_paths,
                strict_context=args.strict_context,
            )
            if not args.quiet:
                for result in results:
                    print(result)
            return 0

    except (EditError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
