import os

import pytest

from app.backend.agents import workspace as ws
from app.backend.agents.workspace import Workspace, WorkspaceError


def build(tmp_path):
    """A small project folder to explore."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("import os\n\ndef main():\n    print('hello world')\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("def helper():\n    return 42\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\nThis is the Hello project.\n", encoding="utf-8")
    (root / "docs" / "notes.txt").write_text("remember the milk\n", encoding="utf-8")
    return root


def refused(call, *args, **kwargs):
    with pytest.raises(WorkspaceError) as error:
        call(*args, **kwargs)

    return str(error.value)


def symlink(link, target, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


# ------------------------------------------------------------ opening it

def test_the_root_must_be_a_folder(tmp_path):
    (tmp_path / "file.txt").write_text("x")

    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "file.txt")

    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "missing")


def test_a_root_inside_a_blocked_place_is_refused(tmp_path):
    secret = tmp_path / "data"
    (secret / "inner").mkdir(parents=True)

    with pytest.raises(WorkspaceError):
        Workspace(secret / "inner", blocked=[secret])


# ------------------------------------------------------ escaping attempts

@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../secret.txt",
        "src/../../secret.txt",
        "..\\secret.txt",
        "src\\..\\..\\secret.txt",
        "/etc/passwd",
        "\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "c:secret",
        "~/secret",
        "file.txt:hidden_stream",
        "src/app.py\x00.txt",
    ],
)
def test_paths_that_could_escape_are_refused(tmp_path, path):
    root = build(tmp_path)
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    space = Workspace(root)

    for call in (space.read_file, space.list_files):
        assert refused(call, path)

    assert refused(space.search_files, "secret", path)


@pytest.mark.parametrize("name", ["CON", "nul", "COM1", "lpt9", "aux.txt", "NUL.log", "con ", "src/PRN"])
def test_windows_device_names_are_refused(tmp_path, name):
    space = Workspace(build(tmp_path))

    assert "reserved" in refused(space.read_file, name)


def test_a_symlinked_file_pointing_outside_is_refused(tmp_path):
    root = build(tmp_path)
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    symlink(root / "link.txt", tmp_path / "secret.txt")
    space = Workspace(root)

    assert "outside" in refused(space.read_file, "link.txt")


def test_a_symlinked_folder_pointing_outside_is_refused_everywhere(tmp_path):
    root = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("TOP SECRET")
    symlink(root / "escape", outside, directory=True)
    space = Workspace(root)

    assert "outside" in refused(space.list_files, "escape")
    assert "outside" in refused(space.read_file, "escape/private.txt")
    assert "escape" not in space.list_files(".")        # not even listed
    assert "escape" not in space.overview()
    assert "TOP SECRET" not in space.search_files("secret")


def test_a_symlink_that_stays_inside_is_fine(tmp_path):
    root = build(tmp_path)
    symlink(root / "alias.py", root / "src" / "app.py")
    space = Workspace(root)

    assert "hello world" in space.read_file("alias.py")


def test_blocked_places_are_invisible_and_unreadable(tmp_path):
    root = build(tmp_path)
    data = root / "data"
    data.mkdir()
    (data / "orion.db").write_text("password hashes")
    space = Workspace(root, blocked=[data])

    assert "off limits" in refused(space.read_file, "data/orion.db")
    assert "off limits" in refused(space.list_files, "data")
    assert "data/" not in space.list_files(".")
    assert "data/" not in space.overview()
    assert "password hashes" not in space.search_files("password")


# ------------------------------------------------------------ list_files

def test_list_shows_folders_first_then_files_with_sizes(tmp_path):
    root = build(tmp_path)
    readme_size = len((root / "README.md").read_bytes())   # differs on Windows (CRLF)

    text = Workspace(root).list_files(".")

    assert text.splitlines()[0] == "Contents of .: 2 folders, 1 files"
    assert text.splitlines()[1:] == ["docs/", "src/", f"README.md ({readme_size} B)"]


def test_list_a_subfolder_and_none_means_the_root(tmp_path):
    space = Workspace(build(tmp_path))

    assert space.list_files("src").splitlines()[0] == "Contents of src/: 0 folders, 2 files"
    assert "app.py" in space.list_files("src/")
    assert "src/" in space.list_files(None) and "src/" in space.list_files("")
    assert "app.py" in space.list_files("./src//")


def test_list_errors_are_clear(tmp_path):
    space = Workspace(build(tmp_path))

    assert "is a file" in refused(space.list_files, "README.md")
    assert "No such folder" in refused(space.list_files, "nope")


def test_list_skips_ignored_and_secret_things(tmp_path):
    root = build(tmp_path)
    for name in (".git", "node_modules", "__pycache__", ".venv"):
        (root / name).mkdir()
        (root / name / "x").write_text("x")
    for name in (".env", ".env.local", "server.pem", "id_rsa", "my.key"):
        (root / name).write_text("secret")
    space = Workspace(root)

    listing = space.list_files(".")

    for hidden in (".git", "node_modules", "__pycache__", ".venv", ".env", "server.pem", "id_rsa", "my.key"):
        assert hidden not in listing
    assert "src/" in listing


def test_list_is_capped(tmp_path):
    root = build(tmp_path)
    many = root / "many"
    many.mkdir()
    for i in range(ws.MAX_LIST_ENTRIES + 25):
        (many / f"f{i:03}.txt").write_text("x")
    space = Workspace(root)

    lines = space.list_files("many").splitlines()

    assert len(lines) == 1 + ws.MAX_LIST_ENTRIES + 1
    assert lines[-1].startswith("... and 25 more")


def test_empty_folder(tmp_path):
    root = build(tmp_path)
    (root / "empty").mkdir()

    assert Workspace(root).list_files("empty").endswith("(empty)")


def test_overview_is_an_indented_tree_limited_in_depth_and_size(tmp_path):
    root = build(tmp_path)
    (root / "src" / "deep" / "deeper").mkdir(parents=True)
    (root / "src" / "deep" / "deeper" / "hidden.py").write_text("x")
    space = Workspace(root)
    notes_size = len((root / "docs" / "notes.txt").read_bytes())

    tree = space.overview().splitlines()

    assert tree[:3] == ["docs/", f"  notes.txt ({notes_size} B)", "src/"]
    assert "  deep/" in tree and "    deeper/" not in tree      # depth limit
    assert tree[-1].startswith("README.md")

    capped = space.overview(max_entries=2).splitlines()
    assert len(capped) == 3 and capped[-1].startswith("... and more")


def test_overview_of_an_empty_folder(tmp_path):
    (tmp_path / "e").mkdir()

    assert Workspace(tmp_path / "e").overview() == "(the folder is empty)"


# ------------------------------------------------------------- read_file

def test_read_numbers_the_lines(tmp_path):
    space = Workspace(build(tmp_path))

    text = space.read_file("src/app.py")

    assert text.splitlines()[0] == "src/app.py (lines 1-4 of 4)"
    assert text.splitlines()[1] == "    1| import os"
    assert text.splitlines()[4] == "    4|     print('hello world')"
    assert "More" not in text


def test_read_pages_through_a_long_file(tmp_path):
    root = build(tmp_path)
    (root / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 301)), encoding="utf-8")
    space = Workspace(root)

    first = space.read_file("long.txt")
    assert first.splitlines()[0] == "long.txt (lines 1-120 of 300)"
    assert "[More: call read_file with start_line=121]" in first

    last = space.read_file("long.txt", start_line=241)
    assert last.splitlines()[0] == "long.txt (lines 241-300 of 300)"
    assert "More" not in last

    assert space.read_file("long.txt", start_line=299, max_lines=1).splitlines()[0] == "long.txt (lines 299-299 of 300)"


def test_read_output_is_capped_by_size_and_tells_you_where_to_continue(tmp_path):
    root = build(tmp_path)
    (root / "wide.txt").write_text("\n".join("x" * 200 for _ in range(100)), encoding="utf-8")

    text = Workspace(root).read_file("wide.txt")

    assert len(text) < ws.MAX_READ_CHARS + 300
    assert "[More: call read_file with start_line=" in text
    assert text.splitlines()[0].startswith("wide.txt (lines 1-")


def test_one_huge_line_is_cut(tmp_path):
    root = build(tmp_path)
    (root / "min.js").write_text("a" * 50_000, encoding="utf-8")

    text = Workspace(root).read_file("min.js")

    assert len(text) < 1000 and text.splitlines()[1].endswith(" ...")


def test_read_error_cases(tmp_path):
    root = build(tmp_path)
    (root / "empty.txt").write_text("")
    (root / ".env").write_text("KEY=secret")
    (root / "private.pem").write_text("-----BEGIN-----")
    space = Workspace(root)

    assert "No such file" in refused(space.read_file, "nope.txt")
    assert "is a folder" in refused(space.read_file, "src")
    assert "secrets file" in refused(space.read_file, ".env")
    assert "secrets file" in refused(space.read_file, "private.pem")
    assert "only 4 lines" in refused(space.read_file, "src/app.py", start_line=99)
    assert space.read_file("empty.txt") == "empty.txt: (empty file)"
    assert "lines 1-4" in space.read_file("src/app.py", start_line=0)       # clamps


def test_binary_and_oversized_files_are_not_shown(tmp_path):
    root = build(tmp_path)
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    (root / "big.txt").write_bytes(b"a" * (ws.MAX_FILE_BYTES + 1))
    space = Workspace(root)

    assert "binary" in refused(space.read_file, "image.png")
    assert "too large" in refused(space.read_file, "big.txt")


def test_text_in_older_encodings_still_reads(tmp_path):
    root = build(tmp_path)
    (root / "legacy.txt").write_bytes("caf\xe9 cr\xe8me".encode("latin-1"))
    (root / "hindi.txt").write_text("\u0928\u092e\u0938\u094d\u0924\u0947 ORION", encoding="utf-8")
    space = Workspace(root)

    assert "caf\xe9 cr\xe8me" in space.read_file("legacy.txt")
    assert "\u0928\u092e\u0938\u094d\u0924\u0947 ORION" in space.read_file("hindi.txt")


def test_read_accepts_windows_style_separators(tmp_path):
    assert "def helper" in Workspace(build(tmp_path)).read_file("src\\util.py")


# ---------------------------------------------------------- search_files

def test_search_finds_lines_case_insensitively(tmp_path):
    space = Workspace(build(tmp_path))

    text = space.search_files("HELLO")

    assert text.splitlines()[0] == '2 matches for "HELLO":'
    assert "README.md:2: This is the Hello project." in text
    assert "src/app.py:4: print('hello world')" in text


def test_search_can_be_limited_to_a_folder_or_a_file(tmp_path):
    space = Workspace(build(tmp_path))

    assert "README" not in space.search_files("hello", "src")
    assert space.search_files("hello", "src/app.py").startswith('1 match for "hello"')
    assert "No matches" in space.search_files("milk", "src")
    assert "docs/notes.txt:1: remember the milk" in space.search_files("milk")


def test_search_with_no_hits_says_how_much_it_looked_at(tmp_path):
    text = Workspace(build(tmp_path)).search_files("zzzzz")

    assert text == 'No matches for "zzzzz" in . (4 files searched).'


def test_search_treats_the_query_as_plain_text_not_a_regex(tmp_path):
    root = build(tmp_path)
    (root / "regex.txt").write_text("a.b and a(b and (a+)+$\n")
    space = Workspace(root)

    assert "regex.txt:1" in space.search_files("a.b")
    assert "regex.txt:1" in space.search_files("(a+)+$")
    assert "No matches" in space.search_files("a.*b")


def test_search_skips_secrets_binaries_big_files_and_ignored_folders(tmp_path):
    root = build(tmp_path)
    (root / ".env").write_text("needle=1")
    (root / "key.pem").write_text("needle")
    (root / "blob.bin").write_bytes(b"\x00needle")
    (root / "huge.txt").write_text("needle " + "x" * ws.MAX_FILE_BYTES)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("needle")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("needle")

    assert "No matches" in Workspace(root).search_files("needle")


def test_search_results_are_capped(tmp_path):
    root = build(tmp_path)
    (root / "many.txt").write_text("\n".join(f"hit {i}" for i in range(100)))

    text = Workspace(root).search_files("hit")

    assert text.splitlines()[0] == f'{ws.MAX_SEARCH_RESULTS} matches for "hit":'
    assert "Stopped after 20 matches" in text
    assert len(text.splitlines()) == ws.MAX_SEARCH_RESULTS + 2


def test_search_long_lines_are_trimmed(tmp_path):
    root = build(tmp_path)
    (root / "wide.txt").write_text("needle " + "y" * 1000)

    line = Workspace(root).search_files("needle").splitlines()[1]

    assert len(line) < ws.SNIPPET_CHARS + 40


def test_search_input_is_validated(tmp_path):
    space = Workspace(build(tmp_path))

    assert "Give some text" in refused(space.search_files, "   ")
    assert "too long" in refused(space.search_files, "x" * 201)
    assert "No such" in refused(space.search_files, "hello", "nope")


def test_search_stops_when_too_many_files(tmp_path, monkeypatch):
    root = build(tmp_path)
    monkeypatch.setattr(ws, "MAX_SEARCH_FILES", 2)

    text = Workspace(root).search_files("zzzzz")

    assert "Only the first 2 files were searched" in text


def test_search_stops_when_out_of_time(tmp_path, monkeypatch):
    root = build(tmp_path)
    ticks = iter([0.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(ws.time, "monotonic", lambda: next(ticks, 100.0))

    assert "took too long" in Workspace(root).search_files("zzzzz")


def test_search_does_not_follow_symlinks_out_of_the_folder(tmp_path):
    root = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("needle outside")
    symlink(root / "escape", outside, directory=True)
    symlink(root / "peek.txt", outside / "x.txt")

    assert "No matches" in Workspace(root).search_files("needle")


# ------------------------------------------------------------ "did you mean"

def nested(tmp_path):
    root = tmp_path / "proj"
    (root / "app" / "backend" / "ai").mkdir(parents=True)
    (root / "app" / "backend" / "main.py").write_text("x\n")
    (root / "app" / "backend" / "ai" / "llama_server.py").write_text("y\n")
    (root / "frontend").mkdir()
    (root / "README.md").write_text("r\n")
    return Workspace(root)


def test_a_folder_missing_its_parent_is_suggested(tmp_path):
    assert refused(nested(tmp_path).list_files, "backend/") == "No such folder: backend/. Did you mean app/backend/?"


def test_a_bare_file_name_finds_the_file_wherever_it_lives(tmp_path):
    space = nested(tmp_path)

    assert refused(space.read_file, "main.py").endswith("Did you mean app/backend/main.py?")
    assert refused(space.read_file, "llama_server.py").endswith("Did you mean app/backend/ai/llama_server.py?")
    assert refused(space.read_file, "ai/llama_server.py").endswith("Did you mean app/backend/ai/llama_server.py?")


def test_suggestions_ignore_case_and_windows_separators(tmp_path):
    space = nested(tmp_path)

    assert "app/backend/main.py" in refused(space.read_file, "BACKEND\\Main.PY")
    assert "app/backend/" in refused(space.list_files, "Backend")


def test_a_path_match_beats_a_name_only_match(tmp_path):
    space = nested(tmp_path)
    (space.root / "other").mkdir()
    (space.root / "other" / "main.py").write_text("z\n")

    message = refused(space.read_file, "backend/main.py")

    assert "app/backend/main.py" in message and "other/main.py" not in message


def test_several_matches_are_offered_but_capped(tmp_path):
    space = nested(tmp_path)
    for name in "abcde":
        (space.root / name).mkdir()
        (space.root / name / "util.py").write_text("u\n")

    message = refused(space.read_file, "util.py")

    assert "Did you mean one of: " in message
    assert message.count("util.py") == 1 + ws.SUGGESTIONS      # the request itself + 3 suggestions


def test_no_hint_when_nothing_looks_right(tmp_path):
    space = nested(tmp_path)

    assert refused(space.read_file, "totally_unrelated.txt") == "No such file: totally_unrelated.txt."
    assert refused(space.list_files, "nothing") == "No such folder: nothing."
    assert refused(space.search_files, "x", "nothing") == "No such file or folder: nothing."


def test_a_file_is_only_suggested_for_file_requests_and_a_folder_for_folder_requests(tmp_path):
    space = nested(tmp_path)

    assert "Did you mean" not in refused(space.list_files, "main.py")        # main.py is a file
    assert "Did you mean" not in refused(space.read_file, "backend")         # backend is a folder
    assert "app/backend/main.py" in refused(space.search_files, "x", "main.py")   # search takes either
    assert "app/backend/" in refused(space.search_files, "x", "backend")


def test_hidden_things_are_never_suggested(tmp_path):
    space = nested(tmp_path)
    root = space.root
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "secret_helper.py").write_text("n\n")
    (root / "app" / ".env").write_text("KEY=1")
    data = root / "data"
    data.mkdir()
    (data / "orion.db").write_text("hashes")
    guarded = Workspace(root, blocked=[data])

    assert "Did you mean" not in refused(guarded.read_file, "secret_helper.py")
    assert "Did you mean" not in refused(guarded.read_file, "orion.db")
    assert "Did you mean" not in refused(guarded.read_file, ".env")           # app/.env exists but is a secret


def test_suggestions_stay_inside_the_folder(tmp_path):
    space = nested(tmp_path)
    (tmp_path / "outside_file.py").write_text("o\n")
    (tmp_path / "outside").mkdir()
    try:
        os.symlink(tmp_path / "outside", space.root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")

    assert "Did you mean" not in refused(space.read_file, "outside_file.py")


def test_looking_for_suggestions_is_bounded(tmp_path, monkeypatch):
    space = nested(tmp_path)

    monkeypatch.setattr(ws, "SUGGEST_MAX_ENTRIES", 1)
    assert refused(space.read_file, "llama_server.py").startswith("No such file: llama_server.py.")

    monkeypatch.setattr(ws, "SUGGEST_MAX_ENTRIES", 5000)
    ticks = iter([0.0] + [100.0] * 50)
    monkeypatch.setattr(ws.time, "monotonic", lambda: next(ticks, 100.0))
    assert "Did you mean" not in refused(space.read_file, "main.py")

