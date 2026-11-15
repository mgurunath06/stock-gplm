"""Synthetic process-level coverage for native Windows and POSIX locks."""
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

from stock_gplm import locking
from stock_gplm.app.db import AuditDB, LockBusy, ProcessLock


def test_timeout_crash_recovery_and_readonly_probe(tmp_path: Path) -> None:
    path = tmp_path / 'writer.lock'
    child = subprocess.Popen([sys._base_executable, '-c',
        'import sys,time; from pathlib import Path; from stock_gplm.app.db import ProcessLock; '
        'lock=ProcessLock(Path(sys.argv[1])).acquire("child"); print("ready",flush=True); time.sleep(60)', str(path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        started = time.monotonic()
        with pytest.raises(LockBusy):
            ProcessLock(path, timeout_seconds=0.2).acquire('contender')
        assert time.monotonic() - started >= 0.18
        with path.open('r') as stream:
            with pytest.raises(BlockingIOError):
                locking.flock(stream.fileno(), locking.LOCK_EX | locking.LOCK_NB)
    finally:
        child.kill()
        child.wait(timeout=10)
    recovered = ProcessLock(path, timeout_seconds=5).acquire('recovered')
    assert recovered.stale_takeover['pid'] == child.pid
    recovered.release()
    assert path.exists()  # never unlink an inode another contender may have opened
    ProcessLock(path).acquire('again').release()


def test_concurrent_writers_preserve_audit_chain(tmp_path: Path) -> None:
    dbpath = tmp_path / 'audit.sqlite'
    AuditDB(dbpath).close()
    code = '''
import sys,time
from pathlib import Path
from stock_gplm.app.db import ProcessLock,AuditDB
root=Path(sys.argv[1])
with ProcessLock(root/'writer.lock', timeout_seconds=20).acquire('worker'):
    db=AuditDB(root/'audit.sqlite')
    for i in range(15):
        db.event('SYNTHETIC_CONCURRENCY', {'worker':sys.argv[2], 'i':i})
        time.sleep(.001)
    db.close()
'''
    workers = [subprocess.Popen([sys._base_executable, '-c', code, str(tmp_path), str(i)]) for i in range(4)]
    assert [p.wait(timeout=30) for p in workers] == [0]*4
    db = AuditDB(dbpath, readonly=True)
    assert db.one('SELECT COUNT(*) n FROM events')['n'] == 60
    assert db.verify_chain()['ok']
    db.close()

def test_relocated_strategy_still_requires_recorded_digest(tmp_path: Path) -> None:
    from stock_gplm.app.library import Library, StrategyError
    db = AuditDB(tmp_path / 'audit.sqlite')
    lib = Library(tmp_path / 'strategies', db)
    doc = {'name': 'portable', 'timeframe': '1d',
           'entry': {'logic': 'AND', 'conditions': [{'feature': 'RSI(period=14).value', 'op': 'lt', 'threshold': 30}]},
           'exit': {'max_hold_bars': 5, 'stop_atr': 2}}
    row = lib.add(doc, immutable=False, origin='test', active=True)
    db.conn.execute('UPDATE strategies SET path=?', ('/old/linux/home/strategies/user/missing.json',))
    db.conn.commit()
    assert lib.get(row['strategy_id'])['_path'] == row['path']
    Path(row['path']).write_text('{}')
    with pytest.raises(StrategyError, match='SHA-256'):
        lib.get(row['strategy_id'])
    db.close()


@pytest.mark.skipif(os.name != 'nt', reason='native CMD workflow')
def test_gpu_verify_never_passes_empty_run_id(tmp_path: Path) -> None:
    import shutil
    shutil.copyfile(Path(__file__).parents[1] / 'windows/gpu-verify.cmd', tmp_path / 'gpu-verify.cmd')
    (tmp_path / 'gplm.cmd').write_text('@echo off\necho %*>>"%~dp0calls.txt"\nexit /b 0\n')
    p = subprocess.run(['cmd.exe', '/d', '/c', str(tmp_path / 'gpu-verify.cmd')], capture_output=True)
    assert p.returncode == 0
    calls = (tmp_path / 'calls.txt').read_text().splitlines()
    assert sum(line.startswith('audit --verify') for line in calls) == 2
    assert not any('--run' in line for line in calls)
