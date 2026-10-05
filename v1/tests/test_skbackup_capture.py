"""Atomic capture contracts, using scratch datasets and the real engine."""
import json
import subprocess

import pytest

from skbackup_support import needs_restic
from test_skbackup_tree import tree_host


@pytest.mark.parametrize('recursive', [True, False])
def test_fake_snapshot_is_atomic_and_parent_omits_child_data(tmp_path, recursive):
    host = tree_host(tmp_path)
    args = ['-r', 'tank/data@capture'] if recursive else [
        'tank/data@capture', 'tank/data/shared@capture', 'tank/data/share/users@capture']
    host.zfs('snapshot', *args, now=123)
    state = json.loads(host.zstate.read_text())
    assert state['snaps']['tank/data/share/users@capture'] == 123
    assert not (host.data/'.zfs/snapshot/capture/share/users/file').exists()
    assert (host.data/'share/users/.zfs/snapshot/capture/file').read_text() == 'user frozen bytes'
    assert ('tank/data/backups@capture' in state['snaps']) == recursive
    # A collision on a child must prevent creation of every requested snapshot.
    host.zfs('snapshot', 'tank/data/share/users@collision')
    before = json.loads(host.zstate.read_text())
    args = ['-r', 'tank/data@collision'] if recursive else [
        'tank/data@collision', 'tank/data/share/users@collision']
    proc = subprocess.run([str(host.bin/'zfs'), 'snapshot', *args], env=host.env(), capture_output=True)
    assert proc.returncode != 0 and json.loads(host.zstate.read_text()) == before


@needs_restic
@pytest.mark.parametrize('excluded', [True, False])
def test_tree_backup_captures_once_without_sanoid_and_restores_children(tmp_path, excluded):
    host = tree_host(tmp_path)
    if not excluded:
        host.conf.write_text(host.conf.read_text()+"SRC_EXCLUDES=''\n")
    host.run('restic', 'init', check=True)
    host.run('restic', 'backup', check=True)
    repeated = host.run('restic', 'backup', check=True)
    assert 'using parent snapshot' in repeated.stdout+repeated.stderr
    state = json.loads(host.zstate.read_text())
    calls = state['snapshot_calls']
    assert len(calls) == 2
    assert all(('-r' in call) == (not excluded) for call in calls)
    provenance = (host.state/'restic-tree-src-users').read_text().splitlines()
    assert len({s.split('@')[1] for s in provenance}) == 1
    assert len(provenance) == (3 if excluded else 4)
    assert not excluded or not host.snaps('tank/data/backups')
    (host.data/'share/users/file').write_text('changed live data')
    restored = host.run('restic', 'restore-test', check=True)
    assert 'sha256-matched=1' in restored.stdout and 'RESULT: PASS' in restored.stdout
    mounts = [e[1] for e in state['events'] if e[0] == 'mount']
    assert [e[1] for e in state['events'] if e[0] == 'umount'] == mounts[::-1]


def test_failed_unmount_retains_capture_and_reports_failure(tmp_path):
    host = tree_host(tmp_path)
    restic = host.bin/'restic'; restic.write_text('#!/bin/sh\nexit 1\n'); restic.chmod(0o755)
    umount = host.bin/'umount'; umount.write_text('#!/bin/sh\nexit 1\n'); umount.chmod(0o755)
    proc = host.run('restic', 'backup')
    assert proc.returncode != 0 and 'could not unmount' in proc.stdout
    state = json.loads(host.zstate.read_text())
    assert len(state['mounts']) == 3 and len(state['snaps']) == 3
    assert set((host.state/'restic-owned-snapshots').read_text().splitlines()) == set(state['snaps'])


@needs_restic
def test_owned_captures_follow_per_set_references_and_keep_external_snapshots(tmp_path):
    host = tree_host(tmp_path)
    host.zfs('snapshot', '-r', 'tank/data@auto-external')
    host.zfs('snapshot', '-r', 'tank/data@skbackup-restic-'+'a'*32)
    (host.root/'etc/restic-sets.conf').write_text('users paths share/users\napps paths shared/app\n')
    host.run('restic', 'init', check=True)
    host.run('restic', 'backup', check=True)
    first = (host.state/'restic-tree-src-users').read_text().splitlines()
    host.run('restic', 'backup', 'users', check=True)
    second = (host.state/'restic-tree-src-users').read_text().splitlines()
    assert first != second and all(s in json.loads(host.zstate.read_text())['snaps'] for s in first+second)
    assert (host.state/'restic-tree-src-apps').read_text().splitlines() == first
    host.run('restic', 'backup', 'apps', check=True)
    state = json.loads(host.zstate.read_text())
    assert not any(s in state['snaps'] for s in first)
    assert all(s in state['snaps'] for s in second)
    assert 'tank/data/share/users@auto-external' in state['snaps']
    assert 'tank/data/share/users@skbackup-restic-'+'a'*32 in state['snaps']
    # A later failed run must retain both last-success references.
    before = state['snaps']
    restic = host.bin/'restic'; restic.write_text('#!/bin/sh\nexit 1\n'); restic.chmod(0o755)
    assert host.run('restic', 'backup').returncode != 0
    assert json.loads(host.zstate.read_text())['snaps'] == before


@pytest.mark.parametrize('failure', ['mount', 'backup', 'capture'])
def test_failed_tree_run_cleans_only_its_owned_capture(tmp_path, failure):
    host = tree_host(tmp_path)
    host.zfs('snapshot', '-r', 'tank/data@auto-external')
    restic = host.bin/'restic'
    restic.write_text('#!/bin/sh\n'+('exit 1\n' if failure == 'backup' else 'exit 0\n'))
    restic.chmod(0o755)
    if failure in ('mount', 'capture'):
        tool = host.bin/('mount' if failure == 'mount' else 'zfs')
        original = tool.read_text().split('\n', 1)[1]
        condition = '"$6" = "'+str(host.mnt/'shared')+'"' if failure == 'mount' else '"$1" = snapshot'
        tool.write_text('#!/bin/sh\nif [ '+condition+' ]; then exit 1; fi\n'+original)
        tool.chmod(0o755)
    proc = host.run('restic', 'backup')
    assert proc.returncode != 0
    state = json.loads(host.zstate.read_text())
    assert state['snaps'] and all(s.endswith('@auto-external') for s in state['snaps'])
    assert not state.get('mounts') and not (host.state/'restic-tree-src-users').exists()
    mounts = [e[1] for e in state['events'] if e[0] == 'mount']
    assert bool(mounts) == (failure != 'capture')
    assert [e[1] for e in state['events'] if e[0] == 'umount'] == mounts[::-1]


def test_tree_capture_does_not_snapshot_volumes(tmp_path):
    from test_skbackup_tree import add_dataset
    host = tree_host(tmp_path)
    add_dataset(host, 'tank/data/disk')
    state = json.loads(host.zstate.read_text()); state['volumes'] = ['tank/data/disk']
    host.zstate.write_text(json.dumps(state))
    host.conf.write_text(host.conf.read_text()+"SRC_EXCLUDES=''\n")
    restic = host.bin/'restic'; restic.write_text('#!/bin/sh\nexit 0\n'); restic.chmod(0o755)
    host.run('restic', 'backup', check=True)
    state = json.loads(host.zstate.read_text())
    assert len(state['snapshot_calls']) == 1 and '-r' not in state['snapshot_calls'][0]
    assert not any(s.startswith('tank/data/disk@') for s in state['snaps'])


def test_tree_backup_refuses_private_data_before_capture(tmp_path):
    from test_skbackup_tree import add_dataset
    host = tree_host(tmp_path)
    add_dataset(host, 'tank/data/users-private')
    proc = host.run('restic', 'backup')
    assert proc.returncode != 0 and 'private' in proc.stderr.lower()
    assert not json.loads(host.zstate.read_text())['snaps']


def test_restore_test_waits_for_backup_capture_lock(tmp_path):
    import fcntl
    import time
    from skbackup_support import BACKUP
    host = tree_host(tmp_path)
    marker = host.root/'repository-read'
    restic = host.bin/'restic'
    restic.write_text('#!/bin/sh\ntouch '+str(marker)+'\n'); restic.chmod(0o755)
    with (host.root/'lock/skbackup-restic.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proc = subprocess.Popen(['bash', str(BACKUP), '--conf', str(host.conf), 'restic', 'restore-test'],
                                env=host.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Wait for the engine to reach the lock; repository access is forbidden.
            time.sleep(0.5)
            assert proc.poll() is None and not marker.exists()
            fcntl.flock(lock, fcntl.LOCK_UN)
            proc.communicate(timeout=10)
            assert marker.exists()
        finally:
            if proc.poll() is None:
                proc.kill(); proc.communicate()
