#!/usr/bin/env python3
"""Convert git patches to searchable JSON edits, then apply those edits.

The JSON format produced by this script is intentionally text-based instead of
line-number-based. Edits search for exact text and use nearby context only to
disambiguate repeated matches.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class EditError(RuntimeError):
    """Raised when an edit cannot be applied safely."""


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


def _parse_hunk_counts(line: str) -> tuple[int, int]:
    match = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
    if match is None:
        raise EditError(f"invalid hunk header: {line.rstrip()}")
    old_count = int(match.group(1) or "1")
    new_count = int(match.group(2) or "1")
    return old_count, new_count


def parse_unified_diff(patch_text: str) -> list[PatchFile]:
    files: list[PatchFile] = []
    current: PatchFile | None = None
    current_hunk: list[PatchLine] | None = None
    last_hunk: list[PatchLine] | None = None
    old_remaining = 0
    new_remaining = 0

    def ensure_file() -> PatchFile:
        nonlocal current
        if current is None:
            current = PatchFile()
        return current

    def finish_line(kind: str) -> None:
        nonlocal current_hunk, old_remaining, new_remaining
        if kind in {" ", "-"}:
            old_remaining -= 1
        if kind in {" ", "+"}:
            new_remaining -= 1
        if old_remaining < 0 or new_remaining < 0:
            raise EditError("hunk contains more lines than its header declares")
        if old_remaining == 0 and new_remaining == 0:
            current_hunk = None

    for raw_line in patch_text.splitlines(keepends=True):
        if raw_line.startswith("\\ No newline at end of file"):
            hunk = current_hunk or last_hunk
            if hunk and hunk[-1].text.endswith("\n"):
                hunk[-1].text = hunk[-1].text[:-1]
            continue

        if current_hunk is not None:
            if raw_line and raw_line[0] in {" ", "+", "-"}:
                current_hunk.append(PatchLine(kind=raw_line[0], text=raw_line[1:]))
                finish_line(raw_line[0])
                continue

            raise EditError(f"unexpected line inside hunk: {raw_line.rstrip()}")

        if raw_line.startswith("diff --git "):
            if current is not None:
                files.append(current)
            old_path, new_path = _parse_git_paths(raw_line)
            current = PatchFile(old_path=old_path, new_path=new_path)
            last_hunk = None
            continue

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

        if raw_line.startswith("@@ "):
            old_remaining, new_remaining = _parse_hunk_counts(raw_line)
            current_hunk = []
            last_hunk = current_hunk
            ensure_file().hunks.append(current_hunk)
            if old_remaining == 0 and new_remaining == 0:
                current_hunk = None
            continue

        last_hunk = None

    if current_hunk is not None:
        raise EditError("hunk ended before all declared lines were read")

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
        content = path.read_text(encoding="utf-8")
        self.cache[path] = content
        return content

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
    edits = script.get("edits")
    if not isinstance(edits, list):
        raise EditError("script must contain an edits list")

    store = FileStore(Path(base_dir), dry_run=dry_run, unsafe_paths=unsafe_paths)
    results: list[str] = []

    for index, raw_edit in enumerate(edits, start=1):
        if not isinstance(raw_edit, dict):
            raise EditError(f"edit #{index} must be an object")

        op = raw_edit.get("op")
        path = store.resolve(_require_string(raw_edit, "path"))

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
            results.append(f"{path}: created")

        elif op == "delete_file":
            expected = raw_edit.get("expected_content")
            content = store.read(path)
            if expected is not None and content != expected:
                raise EditError(f"{path}: file content does not match expected delete content")
            store.delete(path)
            results.append(f"{path}: deleted")

        elif op == "move_file":
            source = store.resolve(_require_string(raw_edit, "from"))
            store.move(source, path)
            results.append(f"{source}: moved to {path}")

        elif op in {"replace", "delete"}:
            old = _require_string(raw_edit, "old")
            new = _require_string(raw_edit, "new") if op == "replace" else ""
            content = store.read(path)
            start, end = _find_unique_span(
                content,
                old,
                raw_edit.get("context") if isinstance(raw_edit.get("context"), dict) else None,
                path,
                strict_context,
            )
            store.write(path, content[:start] + new + content[end:])
            results.append(f"{path}: {op} applied")

        elif op == "insert":
            text = _require_string(raw_edit, "text")
            where = raw_edit.get("where")
            if not isinstance(where, dict):
                raise EditError(f"{path}: insert edit is missing a where object")
            content = store.read(path)
            position = _find_insert_position(content, where, path)
            store.write(path, content[:position] + text + content[position:])
            results.append(f"{path}: insert applied")

        else:
            raise EditError(f"edit #{index} has unsupported op: {op!r}")

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
