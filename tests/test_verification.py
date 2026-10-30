import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from app.backend.agents import verification as v, tasks
from app.backend.agents.snapshot import ProjectSnapshot
from app.backend.agents.changes import ChangeSet
from app.backend.agents.planner import EditPlan
from app.backend.agents.workspace import Workspace
from app.backend.agents.supervisor import StepFailed, RunCancelled


def test_syntax_does_not_import_target(tmp_path):
    (tmp_path / 'app.py').write_text('raise RuntimeError("must not run")\n')
    assert v.syntax_check(tmp_path, ['app.py'])['status'] == 'passed'
    (tmp_path / 'app.py').write_text('print(\n')
    assert v.syntax_check(tmp_path, ['app.py'])['status'] == 'failed'
    (tmp_path / 'app.py').write_text('return 1\n')
    assert v.syntax_check(tmp_path, ['app.py'])['status'] == 'failed'
    assert v.syntax_check(tmp_path, ['new.py', 'data.txt'])['status'] == 'skipped'


def test_invalid_modes_and_interpreters_rejected(tmp_path):
    for extra in [dict(verification='shell'), dict(verification='pytest'),
                  dict(python='python --something')]:
        with pytest.raises(tasks.ParamError):
            tasks.validate_edit_files(dict(folder=str(tmp_path), instruction='Edit', **extra), env={})


def test_default_venv_discovery(tmp_path):
    candidate = tmp_path / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    settings = tasks.validate_edit_files(dict(folder=str(tmp_path), instruction='Edit', verification='pytest'), env={})
    assert settings['python'] == str(candidate)


def test_pytest_pass_fail_and_no_tests(tmp_path):
    test = tmp_path / 'test_sample.py'
    test.write_text('def test_ok():\n    assert True\n')
    result = v.pytest_check(tmp_path, sys.executable, lambda: None, lambda: 30)
    assert result['status'] == 'passed' and result['exit_code'] == 0
    assert result['command'] == [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
    test.write_text('def test_ok():\n    assert False\n')
    result = v.pytest_check(tmp_path, sys.executable, lambda: None, lambda: 30)
    assert result['status'] == 'failed' and result['exit_code'] == 1
    test.unlink()
    result = v.pytest_check(tmp_path, sys.executable, lambda: None, lambda: 30)
    assert result['status'] == 'failed' and result['exit_code'] == 5


def test_timeout_stops_process(tmp_path, monkeypatch):
    (tmp_path / 'test_slow.py').write_text('import time\ndef test_slow():\n    time.sleep(20)\n')
    monkeypatch.setattr(v, 'TIMEOUT', 0.2)
    result = v.pytest_check(tmp_path, sys.executable, lambda: None, lambda: 30)
    assert result['status'] == 'failed' and result['reason'] == 'timeout'


def test_cancellation_stops_process(tmp_path):
    (tmp_path / 'test_slow.py').write_text('import time\ndef test_slow():\n    time.sleep(20)\n')
    def cancel():
        raise RunCancelled()
    with pytest.raises(RunCancelled):
        v.pytest_check(tmp_path, sys.executable, cancel, lambda: 30)


def test_missing_interpreter_reported_as_failure(tmp_path):
    # A local executable test double is not needed: missing interpreter is
    # a real launch failure and must never be reported as a passed check.
    result = v.pytest_check(tmp_path, str(tmp_path / 'missing-python'), lambda: None, lambda: 30)
    assert result['status'] == 'failed' and result['exit_code'] is None


def context(tmp_path, mode='syntax'):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('value = 1\n')
    snap = ProjectSnapshot(Workspace(root), tmp_path / 'copies')
    changes = ChangeSet(snap.workspace, allowed_files=('app.py',))
    state = dict(snapshot=snap, changes=changes, plan=EditPlan('change', ('app.py',), 'check', 'none'),
                 params=dict(verification=mode, python=sys.executable))
    return SimpleNamespace(state=state, check_cancelled=lambda: None, seconds_left=lambda: 30,
                           info=lambda *args: None), snap


def test_no_pytest_execution_without_opt_in(tmp_path, monkeypatch):
    ctx, snap = context(tmp_path)
    monkeypatch.setattr(v, 'pytest_check', lambda *args: pytest.fail('Unapproved command'))
    try:
        assert v.verify(ctx, 'baseline')
        assert ctx.state['baseline_verification'][1]['status'] == 'not run'
    finally:
        snap.cleanup()


def test_tests_rewriting_input_block_verification(tmp_path, monkeypatch):
    ctx, snap = context(tmp_path, 'pytest')
    def rewrite(*args):
        (snap.root / 'app.py').write_text('tampered = True')
        return dict(check='pytest', status='passed')
    monkeypatch.setattr(v, 'pytest_check', rewrite)
    try:
        assert not v.verify(ctx, 'baseline')
        assert ctx.state['baseline_verification'][-1]['check'] == 'input-integrity'
        assert (snap.source.root / 'app.py').read_text() == 'value = 1\n'
    finally:
        snap.cleanup()


def test_invalid_proposal_blocks_security_and_apply(tmp_path):
    ctx, snap = context(tmp_path)
    ctx.state['baseline_ok'] = True
    ctx.state['changes'].replace('app.py', 'value = 1', 'value = (')
    snap.materialize(ctx.state['changes'])
    try:
        with pytest.raises(StepFailed, match='Verification failed'):
            tasks._security_checks(ctx)
        assert not ctx.state['verification_ok']
        assert (snap.source.root / 'app.py').read_text() == 'value = 1\n'
    finally:
        snap.cleanup()


def test_output_limit_fails_check(tmp_path, monkeypatch):
    (tmp_path / 'test_output.py').write_text('def test_output():\n    assert False, "X" * 5000\n')
    monkeypatch.setattr(v, 'MAX_OUTPUT', 100)
    result = v.pytest_check(tmp_path, sys.executable, lambda: None, lambda: 30)
    assert result['status'] == 'failed'
    assert result['reason'] == 'output limit exceeded'
    assert len(result['output'].encode('utf-8')) <= 100


def test_missing_pytest_in_selected_environment_fails(tmp_path):
    import subprocess
    env = tmp_path / 'env'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(env)], check=True)
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    result = v.pytest_check(tmp_path, str(python), lambda: None, lambda: 30)
    assert result['status'] == 'failed' and result['exit_code'] != 0
    assert 'pytest' in result['output']


def test_apply_cannot_bypass_verification_gate(tmp_path):
    ctx, snap = context(tmp_path)
    ctx.state.update(decision='apply', run_id='test', editor_summary='changed')
    ctx.state['changes'].replace('app.py', '1', '2')
    try:
        with pytest.raises(StepFailed, match='Verification has not passed'):
            tasks._apply_changes(ctx)
        assert (snap.source.root / 'app.py').read_text() == 'value = 1\n'
    finally:
        snap.cleanup()
