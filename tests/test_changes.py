import os
import stat

import pytest

from app.backend.agents import changes as ch
from app.backend.agents.changes import (
    ChangeError,
    ChangeSet,
    changes_summary,
    discard_backups,
    read_manifest,
    undo_changes,
)
from app.backend.agents.workspace import Workspace

APP = "import os\n\ndef main():\n    print('hello world')\n    return 1\n"


def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP, newline="\n")
    (root / "README.md").write_bytes(b"# Demo\n")
    return root


def make(tmp_path, **kwargs):
    root = project(tmp_path)

    return ChangeSet(Workspace(root, blocked=kwargs.pop("blocked", [])), **kwargs), root


def refused(call, *args, **kwargs):
    with pytest.raises(ChangeError) as error:
        call(*args, **kwargs)

    return str(error.value)


def symlink(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


def raw(path):
    return path.read_bytes()


# ------------------------------------------------------------------ replacing

def test_one_exact_replacement_is_staged_but_not_written(tmp_path):
    changes, root = make(tmp_path)

    message = changes.replace("src/app.py", "print('hello world')", "print('hello ORION')")

    assert message.startswith("Replaced 1 place in src/app.py. It now reads:")
    assert "    4|     print('hello ORION')" in message
    assert raw(root / "src" / "app.py") == APP.encode()                     # disk untouched
    assert bool(changes) and changes.stats() == (1, 1, 1)


def test_the_diff_is_a_normal_unified_diff(tmp_path):
    changes, _ = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")

    lines = changes.diff().splitlines()

    assert lines[0] == "--- a/src/app.py" and lines[1] == "+++ b/src/app.py"
    assert lines[2].startswith("@@")
    assert "-    return 1" in lines and "+    return 2" in lines
    assert "\r" not in changes.diff()


def test_part_of_a_line_can_be_replaced(tmp_path):
    changes, _ = make(tmp_path)

    changes.replace("src/app.py", "'hello world'", "'hi'")

    assert "print('hi')" in changes.diff()


def test_later_edits_see_earlier_ones(tmp_path):
    changes, _ = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.replace("src/app.py", "return 2", "return 3")

    assert changes.stats() == (1, 1, 1) and "+    return 3" in changes.diff()
    assert "was not found" in refused(changes.replace, "src/app.py", "return 1", "x")      # it is gone now


def test_an_edit_that_undoes_itself_leaves_no_change(tmp_path):
    changes, _ = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.replace("src/app.py", "return 2", "return 1")

    assert not changes and changes.diff() == "" and changes.stats() == (0, 0, 0)


def test_bad_edits_are_explained_and_change_nothing(tmp_path):
    changes, _ = make(tmp_path)

    assert "'find' is empty" in refused(changes.replace, "src/app.py", "", "x")
    assert "'find' is empty" in refused(changes.replace, "src/app.py", None, "x")
    assert "identical" in refused(changes.replace, "src/app.py", "return 1", "return 1")
    assert "was not found" in refused(changes.replace, "src/app.py", "this text is not there", "x")
    assert "matches 2 places" in refused(changes.replace, "src/app.py", "(", "[")        # main( and print(
    assert "A 'path' is needed" in refused(changes.replace, "", "a", "b")
    assert not changes


def test_a_wrong_indentation_gets_the_exact_text_back(tmp_path):
    changes, _ = make(tmp_path)

    message = refused(changes.replace, "src/app.py", "print('hello world')\nreturn 1", "x")

    assert "match apart from spaces or indentation" in message
    assert "from line 4" in message
    assert "    print('hello world')\n    return 1" in message             # the real, indented lines


def test_a_near_miss_names_the_closest_line(tmp_path):
    changes, _ = make(tmp_path)

    message = refused(changes.replace, "src/app.py", "def mian():", "x")

    assert "The closest line is line 3: def main():" in message
    assert "without the line numbers" in message


def test_line_numbers_copied_from_read_file_are_stripped(tmp_path):
    changes, _ = make(tmp_path)

    changes.replace(
        "src/app.py",
        "    4|     print('hello world')\n    5|     return 1",
        "    4|     print('hi')\n    5|     return 2",
    )

    diff = changes.diff()

    assert "+    print('hi')" in diff and "+    return 2" in diff and "4|" not in diff


def test_only_fully_numbered_text_is_treated_as_numbered(tmp_path):
    root = project(tmp_path)
    (root / "table.txt").write_text("1| one\ntwo\n")
    changes = ChangeSet(Workspace(root))

    changes.replace("table.txt", "1| one\ntwo", "1| uno\ntwo")        # not every line is numbered

    assert "+1| uno" in changes.diff()


# -------------------------------------------------------------- line endings etc.

def test_windows_line_endings_are_preserved_on_write(tmp_path):
    root = project(tmp_path)
    (root / "win.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    changes = ChangeSet(Workspace(root))

    changes.replace("win.txt", "beta\ngamma", "BETA\ngamma")           # the model writes plain \n
    changes.apply(tmp_path / "backup")

    assert raw(root / "win.txt") == b"alpha\r\nBETA\r\ngamma\r\n"
    assert "\r" not in changes.diff()


def test_a_byte_order_mark_is_kept(tmp_path):
    root = project(tmp_path)
    (root / "bom.txt").write_bytes(b"\xef\xbb\xbfhello\nworld\n")
    changes = ChangeSet(Workspace(root))

    changes.replace("bom.txt", "world", "there")
    changes.apply(tmp_path / "backup")

    assert raw(root / "bom.txt") == b"\xef\xbb\xbfhello\nthere\n"


def test_unix_files_stay_unix(tmp_path):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.apply(tmp_path / "backup")

    assert b"\r" not in raw(root / "src" / "app.py")


def test_files_with_mixed_or_odd_line_endings_are_refused(tmp_path):
    root = project(tmp_path)
    (root / "mixed.txt").write_bytes(b"a\r\nb\nc\r\n")
    (root / "old_mac.txt").write_bytes(b"a\rb\rc")
    changes = ChangeSet(Workspace(root))

    assert "mixes Windows and Unix" in refused(changes.replace, "mixed.txt", "b", "B")
    assert "unusual line endings" in refused(changes.replace, "old_mac.txt", "b", "B")


def test_a_file_without_a_final_newline_stays_that_way(tmp_path):
    root = project(tmp_path)
    (root / "bare.txt").write_bytes(b"one\ntwo")
    changes = ChangeSet(Workspace(root))

    changes.replace("bare.txt", "two", "three")
    changes.apply(tmp_path / "backup")

    assert raw(root / "bare.txt") == b"one\nthree"


# -------------------------------------------------------------------- refusals

@pytest.mark.parametrize(
    "path, fragment",
    [
        ("../outside.txt", "'..'"),
        ("/etc/passwd", "relative"),
        ("C:\\Windows\\win.ini", "relative"),
        ("src/CON", "reserved"),
        ("src/app.py:stream", "':'"),
        (".env", "secrets file"),
        (".env.local", "secrets file"),
        ("deploy/server.pem", "secrets file"),
        (".git/config", "inside .git"),
        ("node_modules/pkg/index.js", "inside node_modules"),
        (".venv/lib/x.py", "inside .venv"),
        ("src", "is a folder"),
        (".", "folder itself"),
    ],
)
def test_unsafe_targets_are_refused_for_both_replace_and_create(tmp_path, path, fragment):
    changes, root = make(tmp_path)
    (root / "deploy").mkdir()
    (root / ".git").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)

    assert fragment in refused(changes.replace, path, "a", "b")
    assert fragment in refused(changes.create, path, "content\n")
    assert not changes


def test_orions_own_data_is_never_editable(tmp_path):
    root = project(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "orion.db").write_bytes(b"hashes")
    changes = ChangeSet(Workspace(root, blocked=[root / "data"]))

    assert "off limits" in refused(changes.replace, "data/orion.db", "a", "b")
    assert "off limits" in refused(changes.create, "data/new.txt", "x\n")


def test_symlinks_cannot_lead_outside(tmp_path):
    changes, root = make(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("secret\n")
    symlink(root / "link.txt", outside / "target.txt")
    symlink(root / "dir_link", outside, directory=True)

    assert "outside" in refused(changes.replace, "link.txt", "secret", "x")
    assert "outside" in refused(changes.create, "dir_link/new.txt", "x\n")
    assert (outside / "target.txt").read_text() == "secret\n"


def test_files_that_are_unsafe_to_rewrite_are_refused(tmp_path):
    root = project(tmp_path)
    (root / "image.png").write_bytes(b"\x89PNG\x00\x00binary")
    (root / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    (root / "huge.txt").write_bytes(b"a" * (ch.MAX_FILE_BYTES + 1))
    changes = ChangeSet(Workspace(root))

    assert "binary" in refused(changes.replace, "image.png", "a", "b")
    assert "not UTF-8" in refused(changes.replace, "latin.txt", "caf", "cafe")
    assert "too large" in refused(changes.replace, "huge.txt", "a", "b")


def test_a_missing_file_suggests_the_right_one_and_create(tmp_path):
    changes, _ = make(tmp_path)

    message = refused(changes.replace, "app.py", "a", "b")

    assert "No such file: app.py." in message and "Did you mean src/app.py?" in message
    assert "use 'create'" in message


# ----------------------------------------------------------------------- limits

def test_the_number_of_files_is_limited(tmp_path):
    root = project(tmp_path)
    for i in range(4):
        (root / f"f{i}.txt").write_text(f"one {i}\n")
    changes = ChangeSet(Workspace(root), max_files=3)

    for i in range(3):
        changes.replace(f"f{i}.txt", "one", "two")

    assert "At most 3 files" in refused(changes.replace, "f3.txt", "one", "two")
    changes.replace("f0.txt", "two", "three")                           # more edits to a known file are fine


def test_the_number_of_edits_is_limited(tmp_path):
    changes, root = make(tmp_path, max_edits=2)

    changes.replace("src/app.py", "return 1", "return 2")
    changes.replace("src/app.py", "return 2", "return 3")

    assert "At most 2 edits" in refused(changes.replace, "src/app.py", "return 3", "return 4")
    assert "At most 2 edits" in refused(changes.create, "new.txt", "x\n")


def test_an_edit_cannot_make_a_file_huge(tmp_path):
    changes, _ = make(tmp_path)

    assert "too large" in refused(changes.replace, "src/app.py", "return 1", "x" * (ch.MAX_FILE_BYTES + 1))


# ---------------------------------------------------------------------- creating

def test_a_new_file_is_staged_with_a_trailing_newline(tmp_path):
    changes, root = make(tmp_path)

    message = changes.create("docs/notes.txt", "first\r\nsecond")

    assert message == "Created docs/notes.txt (2 lines)."
    assert not (root / "docs").exists()                                   # nothing on disk yet
    diff = changes.diff()
    assert diff.splitlines()[:2] == ["--- /dev/null", "+++ b/docs/notes.txt"]
    assert "+first" in diff and "+second" in diff and "\r" not in diff
    assert changes.stats() == (1, 2, 0)


def test_create_refuses_bad_requests(tmp_path):
    changes, root = make(tmp_path)

    assert "already exists" in refused(changes.create, "src/app.py", "x\n")
    assert "needs some 'content'" in refused(changes.create, "new.txt", "  \n")
    assert "needs some 'content'" in refused(changes.create, "new.txt", None)
    assert "at most" in refused(changes.create, "new.txt", "x" * (ch.MAX_NEW_FILE_CHARS + 1))
    assert "is a file" in refused(changes.create, "README.md/inside.txt", "x\n")

    changes.create("new.txt", "x\n")
    assert "already changed in this task" in refused(changes.create, "new.txt", "y\n")


def test_a_new_file_can_be_adjusted_before_applying(tmp_path):
    changes, root = make(tmp_path)
    changes.create("new.txt", "alpha\nbeta\n")

    changes.replace("new.txt", "beta", "gamma")
    changes.apply(tmp_path / "backup")

    assert raw(root / "new.txt") == b"alpha\ngamma\n"


def test_staged_files_can_be_read_back(tmp_path):
    changes, _ = make(tmp_path)

    assert changes.read("src/app.py") is None                             # not staged: caller reads the disk
    changes.replace("src/app.py", "return 1", "return 2")
    changes.create("new.txt", "a\nb\nc\n")

    text = changes.read("src/app.py")
    assert text.splitlines()[0] == "src/app.py (lines 1-5 of 5) [including your staged changes]"
    assert "    5|     return 2" in text
    assert changes.read("new.txt", start_line=2).splitlines()[1] == "    2| b"
    assert changes.read("../x") is None and changes.read(None) is None


# -------------------------------------------------------------------- applying

def test_applying_writes_files_creates_folders_and_records_a_backup(tmp_path):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.create("docs/new/notes.txt", "hello\n")
    backup = tmp_path / "backup"

    report = changes.apply(backup)

    assert report.written == ["src/app.py"] and report.created == ["docs/new/notes.txt"]
    assert raw(root / "src" / "app.py") == APP.replace("return 1", "return 2").encode()
    assert raw(root / "docs" / "new" / "notes.txt") == b"hello\n"
    assert raw(backup / "files" / "0.bak") == APP.encode()                  # the original, byte for byte

    manifest = read_manifest(backup)
    assert manifest["state"] == "applied" and manifest["undone"] is False
    assert [f["path"] for f in manifest["files"]] == ["src/app.py", "docs/new/notes.txt"]
    assert manifest["files"][0]["before_sha"] != manifest["files"][0]["after_sha"]
    assert manifest["files"][1]["created_dirs"] == ["docs", "docs/new"]
    assert manifest["root"] == str(root.resolve())
    assert not list(root.rglob(".orion-*.tmp"))                             # no temp files left behind


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_permissions_survive_an_edit(tmp_path):
    changes, root = make(tmp_path)
    script = root / "run.sh"
    script.write_text("echo one\n")
    script.chmod(0o755)
    changes = ChangeSet(Workspace(root))

    changes.replace("run.sh", "one", "two")
    changes.apply(tmp_path / "backup")

    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_applying_with_nothing_staged_is_refused(tmp_path):
    changes, _ = make(tmp_path)

    assert "no changes" in refused(changes.apply, tmp_path / "backup")


def test_a_file_changed_since_the_proposal_blocks_everything(tmp_path):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.replace("README.md", "Demo", "Demo app")
    (root / "README.md").write_bytes(b"# Demo\nsomeone edited this meanwhile\n")
    backup = tmp_path / "backup"

    message = refused(changes.apply, backup)

    assert "README.md changed since the proposal" in message and "nothing was written" in message
    assert raw(root / "src" / "app.py") == APP.encode()                     # the other file was NOT written either
    assert not backup.exists()                                              # and no backup was made


def test_other_changes_to_the_disk_since_the_proposal_also_block_applying(tmp_path):
    changes, root = make(tmp_path)
    changes.create("new.txt", "mine\n")
    (root / "new.txt").write_text("someone else's\n")

    assert "created by someone else" in refused(changes.apply, tmp_path / "b1")

    changes2, root2 = make(tmp_path / "second")
    changes2.replace("src/app.py", "return 1", "return 2")
    (root2 / "src" / "app.py").unlink()

    assert "can no longer be read" in refused(changes2.apply, tmp_path / "b2")


def test_a_symlink_swapped_in_after_staging_is_caught_when_applying(tmp_path):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    outside = tmp_path / "elsewhere.py"
    outside.write_text(APP)
    (root / "src" / "app.py").unlink()
    symlink(root / "src" / "app.py", outside)

    assert "outside" in refused(changes.apply, tmp_path / "backup")
    assert outside.read_text() == APP


def test_a_failure_part_way_puts_everything_back(tmp_path, monkeypatch):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.create("brand/new.txt", "fresh\n")
    changes.replace("README.md", "Demo", "Demo app")
    real = ch._atomic_write
    writes = []

    def flaky(path, data):
        writes.append(path.name)

        if path.name == "README.md":                                        # the third write fails
            raise OSError("disk full")

        real(path, data)

    monkeypatch.setattr(ch, "_atomic_write", flaky)
    backup = tmp_path / "backup"

    message = refused(changes.apply, backup)

    assert "Writing failed (disk full)" in message and "put back as they were" in message
    monkeypatch.setattr(ch, "_atomic_write", real)
    assert raw(root / "src" / "app.py") == APP.encode()                     # restored
    assert not (root / "brand").exists()                                    # new file and its folder removed
    assert raw(root / "README.md") == b"# Demo\n"
    assert read_manifest(backup)["state"] == "rolled_back"
    assert changes_summary(backup) is None                                   # nothing to offer undo for


# ------------------------------------------------------------------------- undo

def applied(tmp_path):
    changes, root = make(tmp_path)
    changes.replace("src/app.py", "return 1", "return 2")
    changes.create("docs/new/notes.txt", "hello\n")
    backup = tmp_path / "backup"
    changes.apply(backup)

    return root, backup


def test_undo_restores_edits_and_removes_created_files_and_folders(tmp_path):
    root, backup = applied(tmp_path)

    report = undo_changes(backup)

    assert report.restored == ["src/app.py"] and report.deleted == ["docs/new/notes.txt"] and report.skipped == []
    assert raw(root / "src" / "app.py") == APP.encode()
    assert not (root / "docs").exists()
    assert read_manifest(backup)["undone"] is True


def test_undo_can_only_happen_once(tmp_path):
    _, backup = applied(tmp_path)
    undo_changes(backup)

    assert "already undone" in refused(undo_changes, backup)


def test_undo_never_overwrites_work_done_after_the_change(tmp_path):
    root, backup = applied(tmp_path)
    (root / "src" / "app.py").write_text(APP.replace("return 1", "return 99"))        # the user edits it
    (root / "docs" / "new" / "notes.txt").write_text("I rewrote this\n")

    report = undo_changes(backup)

    assert report.restored == [] and report.deleted == []
    assert [path for path, _ in report.skipped] == ["src/app.py", "docs/new/notes.txt"]
    assert all("left it alone" in why or "left alone" in why for _, why in report.skipped)
    assert "return 99" in (root / "src" / "app.py").read_text()
    assert (root / "docs" / "new" / "notes.txt").read_text() == "I rewrote this\n"
    assert (root / "docs" / "new").exists()                                   # a folder that still has a file stays


def test_undo_copes_with_files_already_put_back_or_removed(tmp_path):
    root, backup = applied(tmp_path)
    (root / "src" / "app.py").write_text(APP, newline="\n")                   # already the original
    (root / "docs" / "new" / "notes.txt").unlink()                            # already gone

    report = undo_changes(backup)

    assert report.restored == ["src/app.py"] and report.deleted == ["docs/new/notes.txt"] and not report.skipped


def test_undo_without_a_record_or_after_a_rollback_is_refused(tmp_path):
    assert "no applied changes" in refused(undo_changes, tmp_path / "nothing")

    backup = tmp_path / "rb"
    backup.mkdir()
    (backup / "manifest.json").write_text('{"files": [], "state": "rolled_back"}')

    assert "no applied changes" in refused(undo_changes, backup)


def test_a_tampered_record_cannot_make_undo_touch_other_places(tmp_path):
    root, backup = applied(tmp_path)
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me\n")
    manifest = read_manifest(backup)
    manifest["files"].append({"path": "../victim.txt", "existed": False, "backup": None,
                              "before_sha": None, "after_sha": ch._sha(b"keep me\n"), "created_dirs": []})
    (backup / "manifest.json").write_text(__import__("json").dumps(manifest))

    report = undo_changes(backup)

    assert outside.read_text() == "keep me\n"
    assert ("../victim.txt", "'..' is not allowed; stay inside the folder.") in report.skipped


def test_undo_respects_off_limits_places(tmp_path):
    root, backup = applied(tmp_path)

    report = undo_changes(backup, blocked=[root / "src"])

    assert [p for p, _ in report.skipped] == ["src/app.py"]
    assert "off limits" in report.skipped[0][1]
    assert report.deleted == ["docs/new/notes.txt"]


def test_the_page_summary_follows_the_manifest(tmp_path):
    assert changes_summary(tmp_path / "none") is None

    _, backup = applied(tmp_path)
    info = changes_summary(backup)

    assert info["applied"] is True and info["undone"] is False
    assert info["files"] == ["src/app.py", "docs/new/notes.txt"] and info["created"]

    undo_changes(backup)
    assert changes_summary(backup)["undone"] is True

    discard_backups(backup)
    assert changes_summary(backup) is None and not backup.exists()
