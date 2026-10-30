import stat
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from app.backend.agents import snapshot as module
from app.backend.agents.snapshot import ProjectSnapshot, is_link
from app.backend.agents.changes import ChangeSet, ChangeError, undo_changes
from app.backend.agents.supervisor import Supervisor, Step, StepFailed, RunCancelled
from app.backend.agents.workspace import Workspace


def symlink_or_skip(link, target, *, directory=False):
    """Skip only unsupported symlinks or Windows' explicit privilege error."""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except NotImplementedError:
        pytest.skip("Symbolic links are not supported on this platform")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation unavailable (WinError 1314); no elevation required")
        raise


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_bytes(b'value = 1\r\n')
    (root / 'helper.py').write_text('helper = True\n')
    return root


def test_copy_materialize_apply_undo(project, tmp_path):
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    changes = ChangeSet(snap.workspace, allowed_files=('app.py', 'new.py'))
    changes.replace('app.py', '1', '2')
    changes.create('new.py', '# new\n')
    snap.materialize(changes)
    assert (snap.root / 'app.py').read_bytes() == b'value = 2\r\n'
    assert (project / 'app.py').read_bytes() == b'value = 1\r\n'
    assert not (project / 'new.py').exists()
    backups = tmp_path / 'backups'
    snap.original_changes(changes).apply(backups)
    assert (project / 'app.py').read_bytes() == b'value = 2\r\n'
    undo_changes(backups)
    assert (project / 'app.py').read_bytes() == b'value = 1\r\n'
    assert not (project / 'new.py').exists()
    snap.cleanup()
    assert not snap.directory.exists()


def test_secrets_dependencies_and_models_excluded(project, tmp_path):
    for folder in ('.git', '.venv', 'models', 'runtime', 'dist'):
        (project / folder).mkdir()
        (project / folder / 'file').write_text('private')
    (project / '.env').write_text('PASSWORD=secret')
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    assert set(snap.hashes) == {'app.py', 'helper.py'}
    snap.cleanup()


@pytest.mark.parametrize('directory', [False, True])
def test_symbolic_links_excluded(project, tmp_path, directory):
    outside = tmp_path / 'outside'
    if directory:
        outside.mkdir()
        (outside / 'private').write_text('outside')
    else:
        outside.write_text('outside')
    symlink_or_skip(project / 'link', outside, directory=directory)
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    try:
        assert set(snap.hashes) == {'app.py', 'helper.py'}
        assert not (snap.root / 'link').exists()
    finally:
        snap.cleanup()


def test_windows_reparse_attribute_is_a_link(monkeypatch):
    fake = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
    assert is_link(fake)


@pytest.mark.parametrize('change', ['edited', 'removed', 'link'])
def test_modified_copied_dependency_refuses_apply(project, tmp_path, change):
    if change == 'link':
        probe = tmp_path / 'symlink-probe'
        symlink_or_skip(probe, project / 'app.py')
        probe.unlink()
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    changes = ChangeSet(snap.workspace, allowed_files=('app.py',))
    changes.replace('app.py', '1', '2')
    path = project / 'helper.py'
    if change == 'edited':
        path.write_text('changed')
    elif change == 'removed':
        path.unlink()
    else:
        path.unlink()
        path.symlink_to(project / 'app.py')
    with pytest.raises((ChangeError, OSError)):
        snap.original_changes(changes)
    assert (project / 'app.py').read_bytes() == b'value = 1\r\n'
    snap.cleanup()


def test_new_approved_file_created_by_user_is_preserved(project, tmp_path):
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    changes = ChangeSet(snap.workspace, allowed_files=('new.py',))
    changes.create('new.py', '# proposed')
    (project / 'new.py').write_text('# user')
    with pytest.raises(ChangeError):
        snap.original_changes(changes).apply(tmp_path / 'backups')
    assert (project / 'new.py').read_text() == '# user'
    snap.cleanup()


def test_failed_copy_cleans_partial_files(project, tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'MAX_FILES', 1)
    parent = tmp_path / 'copies'
    with pytest.raises(ChangeError):
        ProjectSnapshot(Workspace(project), parent)
    assert list(parent.iterdir()) == []


def test_nested_data_folder_not_recursively_copied(project):
    data = project / 'data'
    data.mkdir()
    (data / 'db').write_text('private')
    snap = ProjectSnapshot(Workspace(project, blocked=[data]), data / 'snapshots')
    assert set(snap.hashes) == {'app.py', 'helper.py'}
    snap.cleanup()


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'cancelled'])
def test_cleanup_on_all_supervisor_outcomes(project, tmp_path, outcome):
    snap = ProjectSnapshot(Workspace(project), tmp_path / 'copies')
    def run(ctx):
        if outcome == 'failed':
            raise StepFailed('failure')
        if outcome == 'cancelled':
            raise RunCancelled()
        return 'done'
    runner = Supervisor(SimpleNamespace(emit=lambda _: None), threading.Event(),
                        state={'cleanups': [snap.cleanup]})
    assert runner.run([Step('test', 'test', run)]).status == outcome
    assert not snap.directory.exists()


def test_symlink_helper_skips_windows_privilege_error(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        error = OSError("A required privilege is not held by the client")
        error.winerror = 1314
        raise error
    monkeypatch.setattr(Path, 'symlink_to', denied)
    with pytest.raises(pytest.skip.Exception, match='1314'):
        symlink_or_skip(tmp_path / 'link', tmp_path / 'target')


def test_symlink_helper_does_not_hide_unrelated_errors(monkeypatch, tmp_path):
    def failed(*args, **kwargs):
        raise OSError("Unexpected filesystem error")
    monkeypatch.setattr(Path, 'symlink_to', failed)
    with pytest.raises(OSError, match='Unexpected filesystem error'):
        symlink_or_skip(tmp_path / 'link', tmp_path / 'target')
