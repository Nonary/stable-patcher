from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from json_code_edit import EditError, apply_edit_script, convert_patch_to_json


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
