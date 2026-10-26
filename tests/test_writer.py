"""The one writer (PL1 step 6): one new file in `00 Inbox/`, nothing else. Synthetic
temporary vaults only."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dbrain import capture, writer
from tests.test_capture import NOW


def vault():
    root = Path(tempfile.mkdtemp(prefix="dbrain-writer-"))
    (root / "00 Inbox").mkdir()
    (root / "10 Projects").mkdir()
    (root / "10 Projects/note.md").write_text("---\nclassification: internal\n---\nkeep\n")
    return root


def listing(root):
    out = {}
    for dirpath, _, files in os.walk(str(root)):
        for f in files:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.root = vault()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.name, self.data = capture.render("Read the Mistral paper", "work", NOW)

    def test_one_new_file_and_nothing_else(self):
        before = listing(self.root)
        path = writer.create_inbox_note(self.root, self.name, self.data)
        after = listing(self.root)
        self.assertEqual(path, "00 Inbox/" + self.name)
        self.assertEqual(set(after) - set(before), {path})
        self.assertEqual({k: v for k, v in after.items() if k != path}, before)
        self.assertEqual(after[path], self.data)
        st = os.stat(self.root / path)
        self.assertEqual(st.st_nlink, 1)
        self.assertEqual(st.st_mode & 0o777, 0o644)

    def test_never_overwrites(self):
        (self.root / "00 Inbox" / self.name).write_bytes(b"David's own note")
        path = writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(path, "00 Inbox/" + self.name[:-3] + "-2.md")
        self.assertEqual((self.root / "00 Inbox" / self.name).read_bytes(), b"David's own note")

    def test_an_existing_link_under_the_name_is_not_followed(self):
        target = self.root / "10 Projects/note.md"
        os.symlink(str(target), str(self.root / "00 Inbox" / self.name))
        path = writer.create_inbox_note(self.root, self.name, self.data)
        self.assertTrue(path.endswith("-2.md"))
        self.assertEqual(target.read_bytes(), b"---\nclassification: internal\n---\nkeep\n")

    def test_gives_up_after_nine_names(self):
        stem = self.name[:-3]
        for n in [""] + ["-%d" % i for i in range(2, 10)]:
            (self.root / "00 Inbox" / ("%s%s.md" % (stem, n))).write_bytes(b"x")
        before = listing(self.root)
        with self.assertRaises(writer.WriteRefused):
            writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(listing(self.root), before)

    def test_a_linked_inbox_is_refused(self):
        shutil.rmtree(str(self.root / "00 Inbox"))
        os.symlink(str(self.root / "10 Projects"), str(self.root / "00 Inbox"))
        before = listing(self.root)
        with self.assertRaises(writer.WriteRefused):
            writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(listing(self.root), before)

    def test_a_missing_inbox_is_not_created(self):
        shutil.rmtree(str(self.root / "00 Inbox"))
        with self.assertRaises(writer.WriteRefused):
            writer.create_inbox_note(self.root, self.name, self.data)
        self.assertFalse((self.root / "00 Inbox").exists())

    def test_the_vault_given_through_a_link_resolves_to_its_real_folder(self):
        link = Path(tempfile.mkdtemp(prefix="dbrain-writer-link-")) / "v"
        self.addCleanup(shutil.rmtree, str(link.parent), True)
        os.symlink(str(self.root), str(link))
        path = writer.create_inbox_note(link, self.name, self.data)
        self.assertTrue((self.root / path).is_file())

    def test_a_link_inside_the_real_path_is_never_followed(self):
        """The chain is opened from "/" with O_NOFOLLOW: a component swapped for a link
        after realpath ran is refused, not followed."""
        real = os.path.realpath(str(self.root))
        with mock.patch.object(writer.os.path, "realpath", return_value=real + "-swapped"):
            os.symlink(real, real + "-swapped")
            self.addCleanup(os.unlink, real + "-swapped")
            with self.assertRaises(writer.WriteRefused):
                writer.create_inbox_note(self.root, self.name, self.data)

    def test_names_and_contents_are_validated(self):
        for bad in ("../x.md", "00 Inbox/x.md", ".hidden.md", "2026-10-22-1405-x.txt",
                    "2026-10-22-1405-X.md", "2026-10-22-1405-.md", "x.md", "", None,
                    "2026-10-22-1405-a/../b.md"):
            with self.assertRaises(writer.WriteRefused, msg=bad):
                writer.create_inbox_note(self.root, bad, self.data)
        for bad in (b"no frontmatter", b"---\nclassification: client\n---\n",
                    self.data + b"x" * writer.MAX_NOTE, "text"):
            with self.assertRaises(writer.WriteRefused):
                writer.create_inbox_note(self.root, self.name, bad)
        with self.assertRaises(writer.WriteRefused):
            writer.create_inbox_note(None, self.name, self.data)
        self.assertEqual(os.listdir(str(self.root / "00 Inbox")), [])

    def test_no_temporary_file_is_left_when_the_write_fails(self):
        real_write = os.write
        with mock.patch.object(writer.os, "write", side_effect=OSError("disk full")):
            with self.assertRaises(writer.WriteRefused):
                writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(os.listdir(str(self.root / "00 Inbox")), [])
        self.assertIs(os.write, real_write)

    def test_no_temporary_file_is_left_after_a_success(self):
        writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(os.listdir(str(self.root / "00 Inbox")), [self.name])

    def test_a_note_changed_right_after_placing_is_reported_not_deleted(self):
        real_verify = writer._verify

        def tamper(ifd, name, data):
            with open(str(self.root / "00 Inbox" / name), "ab") as f:
                f.write(b"edited")
            return real_verify(ifd, name, data)

        with mock.patch.object(writer, "_verify", side_effect=tamper):
            with self.assertRaises(writer.NoteChanged):
                writer.create_inbox_note(self.root, self.name, self.data)
        self.assertTrue((self.root / "00 Inbox" / self.name).exists())

    def test_a_temporary_name_already_there_is_never_removed(self):
        """Review 1/2 blocker 1: O_EXCL refusing the temporary name means the file is not
        ours; cleanup must leave it exactly as it was."""
        theirs = self.root / "00 Inbox" / ".capture-0123456789abcdef.tmp"
        theirs.write_bytes(b"not the writer's")
        with mock.patch.object(writer.secrets, "token_hex", return_value="0123456789abcdef"):
            with self.assertRaises(writer.WriteRefused):
                writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(theirs.read_bytes(), b"not the writer's")
        self.assertEqual(sorted(os.listdir(str(self.root / "00 Inbox"))), [theirs.name])

    def test_a_failure_after_placing_says_the_note_exists(self):
        """Review 1/2 blocker 2: the folder sync failing after the link is NoteChanged,
        never a plain refusal, and the note is kept."""
        real_fsync = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:              # 1: the file, 2: the Inbox folder
                raise OSError("sync failed")
            return real_fsync(fd)

        with mock.patch.object(writer.os, "fsync", side_effect=fsync):
            with self.assertRaises(writer.NoteChanged):
                writer.create_inbox_note(self.root, self.name, self.data)
        self.assertEqual(os.listdir(str(self.root / "00 Inbox")), [self.name])
        self.assertEqual((self.root / "00 Inbox" / self.name).read_bytes(), self.data)

    def test_a_temporary_file_that_cannot_be_removed_is_reported(self):
        """Review 1/2 should-fix: a failed write whose cleanup also fails is TempLeft."""
        with mock.patch.object(writer.os, "write", side_effect=OSError("disk full")), \
                mock.patch.object(writer.os, "unlink", side_effect=PermissionError("no")):
            with self.assertRaises(writer.TempLeft):
                writer.create_inbox_note(self.root, self.name, self.data)
        left = os.listdir(str(self.root / "00 Inbox"))
        self.assertEqual(len(left), 1)
        self.assertTrue(left[0].startswith(".capture-"))

    def test_an_unremovable_temporary_name_after_placing_is_note_changed(self):
        with mock.patch.object(writer.os, "unlink", side_effect=PermissionError("no")):
            with self.assertRaises(writer.NoteChanged):
                writer.create_inbox_note(self.root, self.name, self.data)
        self.assertIn(self.name, os.listdir(str(self.root / "00 Inbox")))

    def test_the_writer_has_one_vault_operation(self):
        public = [n for n in dir(writer) if not n.startswith("_") and callable(getattr(writer, n))
                  and getattr(getattr(writer, n), "__module__", "") == writer.__name__]
        self.assertEqual(sorted(public), ["NoteChanged", "TempLeft", "WriteRefused",
                                          "create_inbox_note"])


if __name__ == "__main__":
    unittest.main()
