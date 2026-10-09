"""Stable snapshot trees with dataset mountpoints outside their parents."""
import json
import shutil
import subprocess

import pytest

from skbackup_support import FakeHost, needs_restic
from test_skbackup_tree import add_dataset


def custom_tree(tmp_path, parent_files=False, child_inside=False):
    host = FakeHost(tmp_path, apps=[], sets=['users paths users', 'shared paths nfs-shared',
                                            'projects paths share/projects'],
                    SRC_RECURSIVE=1, RESTORE_TEST_APP='', RESTORE_TEST_SAMPLE_TAG='users')
    users = add_dataset(host, 'tank/data/users', tmp_path/'external/users')
    shared = add_dataset(host, 'tank/data/nfs-shared', tmp_path/'external/nfs-export/shared')
    parent = add_dataset(host, 'tank/data/share', tmp_path/'external/share')
    projects = add_dataset(host, 'tank/data/share/projects',
                           parent/'projects' if child_inside else tmp_path/'external/projects')
    for path in (users, shared, projects):
        (path/'file').write_text(path.name+' frozen bytes')
    if parent_files:
        (parent/'own-file').write_text('parent-owned bytes')
        (host.root/'etc/restic-sets.conf').write_text('users paths users\nshared paths nfs-shared\nprojects paths share\n')
    return host


def restic(host, *args):
    env = dict(host.env(), RESTIC_REPOSITORY=str(host.repo), RESTIC_PASSWORD='test-restic-password')
    return subprocess.run(['restic', *args], env=env, capture_output=True, text=True, check=True)


def clean_mounts(host):
    state = json.loads(host.zstate.read_text())
    mounted = [e[1] for e in state['events'] if e[0] == 'mount']
    unmounted = [e[1] for e in state['events'] if e[0] == 'umount']
    assert mounted and unmounted == mounted[::-1]
    assert not state['mounts']
    assert not list(host.state.glob('restic-source.*'))
    return state


@needs_restic
def test_custom_mountpoints_use_stable_tree_and_reuse_seed_parent(tmp_path):
    host = custom_tree(tmp_path)
    host.run('restic', 'init', check=True)
    users = tmp_path/'external/users'
    shutil.copytree(users, host.mnt/'users')
    restic(host, 'backup', '--host', 'test-host', '--tag', 'users', '--tag', 'skbackup',
           str(host.mnt/'users'))
    seed = json.loads(restic(host, 'snapshots', '--tag', 'users', '--json').stdout)[0]['id']
    shutil.rmtree(host.mnt)
    proc = host.run('restic', 'backup', check=True)
    assert 'using parent snapshot '+seed[:8] in proc.stdout+proc.stderr
    snapshots = json.loads(restic(host, 'snapshots', '--tag', 'users', '--json').stdout)
    assert len(snapshots) == 2
    latest = next(s for s in snapshots if s['id'] != seed)
    assert latest['parent'] == seed and latest['paths'] == [str(host.mnt/'users')]
    for tag, relative, expected in [('users', 'users', 'users'), ('shared', 'nfs-shared', 'shared'),
                                    ('projects', 'share/projects', 'projects')]:
        dest = tmp_path/('restore-'+tag)
        restic(host, 'restore', 'latest', '--tag', tag, '--target', str(dest))
        assert (dest/str(host.mnt).lstrip('/')/relative/'file').read_text() == expected+' frozen bytes'
    (users/'file').write_text('live changed bytes')
    restored = host.run('restic', 'restore-test', check=True)
    assert 'sha256-matched=1' in restored.stdout and 'RESULT: PASS' in restored.stdout
    state = clean_mounts(host)
    mounts = [e for e in state['events'] if e[0] == 'mount']
    assert mounts[0][1:3] == [str(host.mnt), 'tmpfs']
    stable = {e[3].split('@')[0]: e[1] for e in mounts if e[2] == 'zfs' and e[1].startswith(str(host.mnt)+'/')}
    assert stable == {'tank/data/nfs-shared': str(host.mnt/'nfs-shared'),
                      'tank/data/share/projects': str(host.mnt/'share/projects'),
                      'tank/data/users': str(host.mnt/'users')}
    provenance = (host.state/'restic-tree-src-users').read_text().splitlines()
    assert len(provenance) == 5 and len({s.split('@')[1] for s in provenance}) == 1


@needs_restic
def test_file_holding_parent_keeps_own_files_and_child_mount(tmp_path):
    host = custom_tree(tmp_path, parent_files=True, child_inside=True)
    host.run('restic', 'init', check=True)
    host.run('restic', 'backup', check=True)
    dest = tmp_path/'restore'
    restic(host, 'restore', 'latest', '--tag', 'projects', '--target', str(dest))
    source = dest/str(host.mnt).lstrip('/')/'share'
    assert (source/'own-file').read_text() == 'parent-owned bytes'
    assert (source/'projects/file').read_text() == 'projects frozen bytes'
    state = clean_mounts(host)
    mounts = [e for e in state['events'] if e[0] == 'mount']
    assert any(e[1] == str(host.mnt/'share') and e[3].startswith('tank/data/share@') for e in mounts)


@pytest.mark.parametrize('unsafe', ['missing', 'symlink'])
def test_file_holding_parent_refuses_unavailable_child_directory(tmp_path, unsafe):
    host = custom_tree(tmp_path, parent_files=True)
    if unsafe == 'symlink':
        (tmp_path/'external/share/projects').symlink_to(tmp_path/'external/projects', target_is_directory=True)
    shim = host.bin/'restic'; shim.write_text('#!/bin/sh\nexit 0\n'); shim.chmod(0o755)
    proc = host.run('restic', 'backup')
    assert proc.returncode != 0
    assert ('symlink mount directory refused' if unsafe == 'symlink' else 'snapshot mount directory missing') in proc.stderr
    state = clean_mounts(host)
    assert not state['snaps'] and not (host.state/'restic-last-ok-users').exists()
    assert (tmp_path/'external/share/own-file').read_text() == 'parent-owned bytes'


@pytest.mark.parametrize('failure', ['tmpfs', 'probe', 'inspect', 'child', 'backup', 'signal'])
def test_mount_tree_trap_cleans_partial_failures_and_signals(tmp_path, failure):
    host = custom_tree(tmp_path)
    shim = host.bin/'restic'
    if failure == 'signal':
        shim.write_text('#!/bin/sh\ncase " $* " in *" backup "*) kill -TERM "$PPID";; esac\nexit 0\n')
    else:
        shim.write_text('#!/bin/sh\n'+('exit 1\n' if failure == 'backup' else 'exit 0\n'))
    shim.chmod(0o755)
    if failure in ('tmpfs', 'probe', 'child'):
        mount = host.bin/'mount'; original = mount.read_text().split('\n', 1)[1]
        pattern = {'tmpfs': str(host.mnt), 'probe': str(host.state/'restic-source.')+'*',
                   'child': str(host.mnt/'nfs-shared')}[failure]
        mount.write_text('#!/bin/sh\ncase "$6" in '+pattern+') echo "injected mount failure" >&2; exit 1;; esac\n'+original)
    if failure == 'inspect':
        find = host.bin/'find'
        find.write_text('#!/bin/sh\ncase "$1" in '+str(host.state/'restic-source.')+'*) exit 1;; esac\nexec /usr/bin/find "$@"\n')
        find.chmod(0o755)
    proc = host.run('restic', 'backup')
    assert proc.returncode == (143 if failure == 'signal' else 1)
    if failure == 'inspect':
        assert 'snapshot inspection failed' in proc.stderr
    state = json.loads(host.zstate.read_text())
    assert not state.get('mounts') and not state['snaps']
    assert not (host.state/'restic-last-ok-users').exists()
    assert not list(host.state.glob('restic-source.*'))
    mounts = [e[1] for e in state['events'] if e[0] == 'mount']
    assert [e[1] for e in state['events'] if e[0] == 'umount'] == mounts[::-1]
    if failure != 'tmpfs':
        assert mounts[0] == str(host.mnt)
    if failure == 'probe':
        assert len(mounts) == 1 and 'injected mount failure' in proc.stderr


@needs_restic
@pytest.mark.parametrize('filename', ['own-file', '\n'], ids=['ordinary', 'newline'])
def test_file_holding_root_stacks_on_tmpfs_and_unwinds_both(tmp_path, filename):
    host = custom_tree(tmp_path)
    (host.data/filename).write_text('root-owned bytes')
    for relative in ('users', 'nfs-shared', 'share/projects'):
        (host.data/relative).mkdir(parents=True)
    (host.root/'etc/restic-sets.conf').write_text('root paths .\n')
    host.run('restic', 'init', check=True)
    host.run('restic', 'backup', check=True)
    dest = tmp_path/'restore'
    restic(host, 'restore', 'latest', '--tag', 'root', '--target', str(dest))
    source = dest/str(host.mnt).lstrip('/')
    assert (source/filename).read_text() == 'root-owned bytes'
    assert (source/'users/file').read_text() == 'users frozen bytes'
    assert (source/'nfs-shared/file').read_text() == 'shared frozen bytes'
    assert (source/'share/projects/file').read_text() == 'projects frozen bytes'
    state = clean_mounts(host)
    root_mounts = [e for e in state['events'] if e[0] == 'mount' and e[1] == str(host.mnt)]
    assert [e[2] for e in root_mounts] == ['tmpfs', 'zfs']


def test_mount_tree_refuses_to_cover_an_existing_mount(tmp_path):
    host = custom_tree(tmp_path)
    host.mnt.mkdir(parents=True)
    subprocess.run([str(host.bin/'mount'), '-t', 'tmpfs', '-o', 'mode=0700,nosuid,nodev,noexec',
                    'tmpfs', str(host.mnt)], env=host.env(), check=True)
    shim = host.bin/'restic'; shim.write_text('#!/bin/sh\nexit 0\n'); shim.chmod(0o755)
    proc = host.run('restic', 'backup')
    assert proc.returncode == 1 and 'snapshot mount path already in use' in proc.stderr
    state = json.loads(host.zstate.read_text())
    assert set(state['mounts']) == {str(host.mnt)} and not state['snaps']
    assert not any(e[0] == 'umount' for e in state['events'])
    assert not (host.state/'restic-last-ok-users').exists()


def test_uncertain_unmount_status_retains_frozen_sources(tmp_path):
    host = custom_tree(tmp_path)
    shim = host.bin/'restic'; shim.write_text('#!/bin/sh\nexit 1\n'); shim.chmod(0o755)
    mountpoint = host.bin/'mountpoint'
    original = mountpoint.read_text().split('\n', 1)[1]
    mountpoint.write_text('#!/bin/sh\nif [ -e '+str(host.state/'restic-source-status-fault')+' ]; then exit 2; fi\n'+original)
    # Introduce the status failure only after all mounts have been assembled.
    shim.write_text('#!/bin/sh\ncase " $* " in *" backup "*) touch '+str(host.state/'restic-source-status-fault')+'; exit 1;; esac\nexit 0\n')
    proc = host.run('restic', 'backup')
    assert proc.returncode != 0
    state = json.loads(host.zstate.read_text())
    assert state['mounts'] and len(state['snaps']) == 5
    assert set((host.state/'restic-owned-snapshots').read_text().splitlines()) == set(state['snaps'])
    assert 'cannot inspect mount' in proc.stdout


def test_initial_mount_status_error_refuses_mount_assembly(tmp_path):
    host = custom_tree(tmp_path)
    shim = host.bin/'restic'; shim.write_text('#!/bin/sh\nexit 0\n'); shim.chmod(0o755)
    mountpoint = host.bin/'mountpoint'; mountpoint.write_text('#!/bin/sh\nexit 2\n')
    proc = host.run('restic', 'backup')
    assert proc.returncode == 1 and 'cannot inspect mount' in proc.stderr
    state = json.loads(host.zstate.read_text())
    assert not any(e[0] == 'mount' for e in state['events']) and not state['snaps']
