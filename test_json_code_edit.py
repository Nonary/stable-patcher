from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from json_code_edit import (
    EditError,
    apply_edit_script,
    build_script_from_dirs,
    build_script_from_git,
    convert_patch_to_json,
)


class JsonCodeEditTests(unittest.TestCase):
    def test_convert_and_apply_replace_without_line_numbers(self) -> None:
        patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,3 +1,3 @@
 def greet():
-    return "hello"
+    return "hi"
 
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "example.py"
            target.write_text('# shifted line numbers\n\ndef greet():\n    return "hello"\n\n', encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '# shifted line numbers\n\ndef greet():\n    return "hi"\n\n',
            )

    def test_wrong_hunk_counts_are_recounted_from_body(self) -> None:
        patch = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -999,999 +999,999 @@
 def greet():
-    return "hello"
+    return "hi"
 
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "example.py"
            target.write_text('# shifted line numbers\n\ndef greet():\n    return "hello"\n\n', encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '# shifted line numbers\n\ndef greet():\n    return "hi"\n\n',
            )

    def test_crlf_files_keep_crlf_line_endings(self) -> None:
        patch = """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,2 @@
-alpha
+beta
 gamma
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_bytes(b"alpha\r\ngamma\r\n")

            apply_edit_script(script, root)

            self.assertEqual(target.read_bytes(), b"beta\r\ngamma\r\n")

    def test_replace_is_idempotent_when_already_applied(self) -> None:
        patch = """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,2 @@
-alpha
+beta
 gamma
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("beta\ngamma\n", encoding="utf-8")

            results = apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "beta\ngamma\n")
            self.assertIn("already up to date", results[0])

    def test_insert_is_idempotent_when_already_applied(self) -> None:
        patch = """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,3 @@
 alpha
+beta
 gamma
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

            results = apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nbeta\ngamma\n")
            self.assertIn("already up to date", results[0])

    def test_from_dirs_adds_hashes_and_verifies_exact_apply(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "old"
            new_dir = root / "new"
            apply_dir = root / "apply"
            old_dir.mkdir()
            new_dir.mkdir()
            apply_dir.mkdir()
            (old_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
            (new_dir / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
            (apply_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")

            script = build_script_from_dirs(old_dir, new_dir)
            results = apply_edit_script(script, apply_dir)

            self.assertIn("files", script)
            self.assertEqual((apply_dir / "app.py").read_text(encoding="utf-8"), 'def greet():\n    return "hi"\n')
            self.assertTrue(any("verified final hash" in result for result in results))

    def test_from_git_builds_script_with_base_commit_and_hashes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, stdout=subprocess.PIPE).stdout.decode().strip()

            (root / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
            (root / "new.txt").write_text("new\n", encoding="utf-8")

            script = build_script_from_git(root)

            self.assertEqual(script["base_commit"], base)
            self.assertEqual(len(script["files"]), 2)
            self.assertTrue(any(edit["op"] == "replace" for edit in script["edits"]))
            self.assertTrue(any(edit["op"] == "create_file" and edit["path"] == "new.txt" for edit in script["edits"]))

            apply_dir = root / "apply"
            apply_dir.mkdir()
            (apply_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")

            results = apply_edit_script(script, apply_dir)

            self.assertEqual((apply_dir / "app.py").read_text(encoding="utf-8"), 'def greet():\n    return "hi"\n')
            self.assertEqual((apply_dir / "new.txt").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(any("verified final hash" in result for result in results))

    def test_hash_metadata_allows_safe_drift_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "old"
            new_dir = root / "new"
            apply_dir = root / "apply"
            old_dir.mkdir()
            new_dir.mkdir()
            apply_dir.mkdir()
            (old_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
            (new_dir / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
            (apply_dir / "app.py").write_text('import logging\n\ndef greet():\n    return "hello"\n', encoding="utf-8")

            script = build_script_from_dirs(old_dir, new_dir)
            results = apply_edit_script(script, apply_dir)

            self.assertEqual(
                (apply_dir / "app.py").read_text(encoding="utf-8"),
                'import logging\n\ndef greet():\n    return "hi"\n',
            )
            self.assertTrue(any("drift mode" in result for result in results))
            self.assertFalse(any("verified final hash" in result for result in results))

    def test_hash_metadata_treats_whole_file_match_as_already_applied(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "old"
            new_dir = root / "new"
            apply_dir = root / "apply"
            old_dir.mkdir()
            new_dir.mkdir()
            apply_dir.mkdir()
            (old_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
            (new_dir / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
            (apply_dir / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")

            script = build_script_from_dirs(old_dir, new_dir)
            results = apply_edit_script(script, apply_dir)

            self.assertEqual((apply_dir / "app.py").read_text(encoding="utf-8"), 'def greet():\n    return "hi"\n')
            self.assertEqual(sum("whole file hash matched" in result for result in results), 1)

    def test_hash_metadata_fails_on_real_drift_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "old"
            new_dir = root / "new"
            apply_dir = root / "apply"
            old_dir.mkdir()
            new_dir.mkdir()
            apply_dir.mkdir()
            (old_dir / "app.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
            (new_dir / "app.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
            (apply_dir / "app.py").write_text('def greet():\n    return get_greeting()\n', encoding="utf-8")

            script = build_script_from_dirs(old_dir, new_dir)
            with self.assertRaisesRegex(EditError, "target text was not found"):
                apply_edit_script(script, apply_dir)

    def test_insert_uses_surrounding_text_anchors(self) -> None:
        patch = """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,3 @@
 alpha
+beta
 gamma
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("intro\nalpha\ngamma\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "intro\nalpha\nbeta\ngamma\n")

    def test_delete_removes_exact_text(self) -> None:
        patch = """diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,3 +1,2 @@
 alpha
-beta
 gamma
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "alpha\ngamma\n")

    def test_hunk_lines_that_look_like_file_headers_remain_content(self) -> None:
        patch = """diff --git a/schema.sql b/schema.sql
--- a/schema.sql
+++ b/schema.sql
@@ -1,3 +1,3 @@
 keep
--- old section
+++ new section
 tail
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "schema.sql"
            target.write_text("keep\n-- old section\ntail\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n++ new section\ntail\n")

    def test_create_file_from_new_file_patch(self) -> None:
        patch = """diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+print("new")
+
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)

            apply_edit_script(script, root)

            self.assertEqual((root / "pkg" / "new.py").read_text(encoding="utf-8"), 'print("new")\n\n')

    def test_paths_with_spaces_are_preserved(self) -> None:
        patch = """diff --git a/file with spaces.txt b/file with spaces.txt
--- a/file with spaces.txt
+++ b/file with spaces.txt
@@ -1 +1 @@
-old
+new
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "file with spaces.txt"
            target.write_text("old\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_git_quoted_path_escapes_are_decoded(self) -> None:
        patch = '''diff --git "a/tab\\tfile.txt" "b/tab\\tfile.txt"
--- "a/tab\\tfile.txt"
+++ "b/tab\\tfile.txt"
@@ -1 +1 @@
-old
+new
'''
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tab\tfile.txt"
            target.write_text("old\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_delete_file_from_deleted_file_patch(self) -> None:
        patch = """diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-one
-two
"""
        script = convert_patch_to_json(patch)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "old.txt"
            target.write_text("one\ntwo\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertFalse(target.exists())

    def test_binary_new_file_patch_is_rejected(self) -> None:
        patch = """diff --git a/image.bin b/image.bin
new file mode 100644
index 0000000..1111111
GIT binary patch
literal 1
Ic${Nk000310RR91

literal 0
Hc$@<O00001
"""

        with self.assertRaisesRegex(EditError, "binary patches are not supported"):
            convert_patch_to_json(patch)

    def test_binary_modified_file_patch_is_rejected(self) -> None:
        patch = """diff --git a/image.bin b/image.bin
index 1111111..2222222 100644
Binary files a/image.bin and b/image.bin differ
"""

        with self.assertRaisesRegex(EditError, "binary patches are not supported"):
            convert_patch_to_json(patch)

    def test_context_disambiguates_repeated_text(self) -> None:
        script = {
            "version": 1,
            "edits": [
                {
                    "op": "replace",
                    "path": "sample.txt",
                    "old": "same\n",
                    "new": "changed\n",
                    "context": {"before": "target\n", "after": "tail\n"},
                }
            ],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("other\nsame\ntail\ntarget\nsame\ntail\n", encoding="utf-8")

            apply_edit_script(script, root)

            self.assertEqual(target.read_text(encoding="utf-8"), "other\nsame\ntail\ntarget\nchanged\ntail\n")

    def test_ambiguous_match_without_context_is_rejected(self) -> None:
        script = {
            "version": 1,
            "edits": [
                {"op": "replace", "path": "sample.txt", "old": "same\n", "new": "changed\n", "context": {}}
            ],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.txt").write_text("same\nsame\n", encoding="utf-8")

            with self.assertRaises(EditError):
                apply_edit_script(script, root)


if __name__ == "__main__":
    unittest.main()
