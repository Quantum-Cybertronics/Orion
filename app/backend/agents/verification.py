"""Explicit verification presets. Execution is NOT sandboxed."""
import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from app.backend.agents.supervisor import StepFailed
from app.backend.agents.snapshot import is_link

TIMEOUT = 120
MAX_OUTPUT = 256 * 1024


def syntax_check(root, paths):
    errors = []
    checked = []
    for name in paths:
        if not name.lower().endswith('.py'):
            continue
        path = root / name
        if not path.exists():  # approved new file, absent at baseline
            continue
        checked.append(name)
        try:
            tree = ast.parse(path.read_bytes(), filename=name)
            compile(tree, name, "exec", dont_inherit=True)
        except (SyntaxError, UnicodeError, ValueError) as exc:
            errors.append(f'{name}: {exc}')
    return dict(check='syntax', status='failed' if errors else ('passed' if checked else 'skipped'),
                files=checked, interpreter='ORION host Python syntax compiler', output='\n'.join(errors))


def stop_tree(process):
    if os.name == 'nt':
        subprocess.run([str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'),
                        '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def pytest_check(root, python, check_cancelled, seconds_left):
    command = [python, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']
    env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(name, None)
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    started = time.monotonic()
    limit = min(TIMEOUT, max(0, seconds_left()))
    if limit <= 0:
        raise StepFailed('No run time remains for verification.')
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != 'nt')
        except OSError as exc:
            return dict(check='pytest', status='failed', command=command, exit_code=None,
                        output=f'Could not start selected interpreter: {exc}')
        reason = None
        try:
            while process.poll() is None:
                check_cancelled()
                if time.monotonic() - started >= limit:
                    reason = 'timeout'
                    break
                if os.fstat(log.fileno()).st_size > MAX_OUTPUT:
                    reason = 'output limit exceeded'
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                stop_tree(process)
        log.seek(0)
        output = log.read(MAX_OUTPUT).decode('utf-8', errors='replace')
        if os.fstat(log.fileno()).st_size > MAX_OUTPUT:
            reason = reason or 'output limit exceeded'
        return dict(check='pytest', status='passed' if process.returncode == 0 and not reason else 'failed',
                    command=command, exit_code=process.returncode,
                    duration_seconds=round(time.monotonic() - started, 2),
                    output=output, reason=reason)


def fingerprint(snapshot, extra=()):
    """Verification may create outputs but must not rewrite copied source inputs."""
    from app.backend.agents.snapshot import digest
    result = {}
    for name in set(snapshot.hashes) | set(extra):
        path = snapshot.root / name
        if not path.is_file() or is_link(path):
            raise StepFailed(f'Working-copy input disappeared or became a link: {name}')
        result[name] = digest(path.read_bytes())
    return result


def verify(ctx, phase):
    snapshot = ctx.state['snapshot']
    settings = ctx.state['params']
    files = ctx.state['plan'].files if phase == 'baseline' else [c.path for c in ctx.state['changes'].files()]
    results = [syntax_check(snapshot.root, files)]
    if settings.get('verification', 'syntax') == 'pytest':
        integrity_files = [name for name in files if (snapshot.root / name).exists()]
        before = fingerprint(snapshot, integrity_files)
        results.append(pytest_check(snapshot.root, settings['python'], ctx.check_cancelled, ctx.seconds_left))
        if fingerprint(snapshot, integrity_files) != before:
            results.append(dict(check='input-integrity', status='failed',
                                output='Tests rewrote copied project inputs. Apply is blocked.'))
    else:
        results.append(dict(check='pytest', status='not run', output='Not selected or approved.'))
    ctx.state[phase + '_verification'] = results
    from app.backend.agents.checks import redact_secrets
    ctx.info(f'{phase.title()} verification', redact_secrets(json.dumps(results, indent=2)))
    return not any(r['status'] == 'failed' for r in results)
