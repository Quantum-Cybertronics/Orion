"""Bounded project copies. This is file isolation, NOT an execution sandbox."""
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import replace
from pathlib import Path

from app.backend.agents.changes import ChangeError, ChangeSet
from app.backend.agents.workspace import IGNORED_DIRS, Workspace, is_secret_name

EXCLUDED_DIRS = IGNORED_DIRS | {"models", "runtime", "build", "dist", "coverage", ".cache"}
MAX_FILES = 20_000
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class ProjectSnapshot:
    def __init__(self, source: Workspace, parent: Path, check=lambda: None):
        self.source = source
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="orion-snapshot-", dir=parent))
        self.root = self.directory / "project"
        self.root.mkdir()
        self.hashes = {}
        self.skipped = []
        self.check = check
        try:
            total = 0
            for folder, dirs, names in os.walk(source.root, followlinks=False):
                check()
                here = Path(folder)
                keep = []
                for name in sorted(dirs):
                    path = here / name
                    if (name in EXCLUDED_DIRS or is_secret_name(name) or is_link(path)
                            or path.resolve() == parent.resolve()
                            or parent.resolve() in path.resolve().parents
                            or any(path.resolve() == b or b in path.resolve().parents for b in source.blocked)):
                        self.skipped.append(source.display(path))
                    else:
                        keep.append(name)
                dirs[:] = keep
                for name in sorted(names):
                    check()
                    path = here / name
                    shown = source.display(path)
                    if is_secret_name(name) or is_link(path) or not stat.S_ISREG(path.lstat().st_mode):
                        self.skipped.append(shown)
                        continue
                    if path.stat().st_size > MAX_FILE_BYTES:
                        self.skipped.append(shown)
                        continue
                    data = path.read_bytes()
                    if is_link(path) or len(data) > MAX_FILE_BYTES:
                        raise ChangeError(f"{shown} changed while copying; retry the task.")
                    total += len(data)
                    if len(self.hashes) >= MAX_FILES or total > MAX_BYTES:
                        raise ChangeError("Project exceeds snapshot limits (20,000 files / 256 MiB).")
                    target = self.root / shown
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    self.hashes[shown] = digest(data)
            self.validate_originals()
            self.workspace = Workspace(self.root)
        except BaseException:
            self.cleanup()
            raise

    def validate_originals(self):
        """Refuse stale copied files, including unchanged dependencies read by the agent."""
        for shown, expected in self.hashes.items():
            self.check()
            path = self.source.resolve(shown)
            # Refuse symlinks/junctions in every relative component.
            cursor = self.source.root
            for part in Path(shown).parts:
                cursor = cursor / part
                if is_link(cursor):
                    raise ChangeError(f"{shown} became a link; nothing was written.")
            if digest(path.read_bytes()) != expected:
                raise ChangeError(f"{shown} changed since the snapshot; nothing was written. Run the task again.")

    def materialize(self, changes: ChangeSet):
        """Write only staged approved changes into the disposable copy."""
        if changes:
            changes.apply(self.directory / "staging-backups")

    def original_changes(self, changes: ChangeSet):
        self.validate_originals()
        result = ChangeSet(self.source, allowed_files=tuple(changes.allowed_files or ()))
        for change in changes.files():
            result._check_scope(change.path)
            # Preserve the snapshot baseline, not a newly read original baseline.
            result._changes[change.path] = replace(change, absolute=self.source.resolve(change.path))
        return result

    def cleanup(self):
        shutil.rmtree(self.directory)
