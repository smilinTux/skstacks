"""Dataset-tree contracts exercised through the real engine and deploy."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from skbackup_support import FakeHost, ansible_render, full_vault, needs_ansible, needs_restic, rendered


def add_dataset(host, dataset, mount=None):
    state = json.loads(host.zstate.read_text())
    path = Path(mount or host.root / dataset)
    path.mkdir(parents=True, exist_ok=True)
    state['datasets'][dataset] = str(path)
    host.zstate.write_text(json.dumps(state))
    return path


def tree_host(tmp_path, **conf):
    host = FakeHost(tmp_path, apps=['app shared/app 7 4 3'], sets=['users paths share/users'],
                    SRC_RECURSIVE=1, SRC_EXCLUDES='tank/data/backups', RESTORE_TEST_APP='',
                    RESTORE_TEST_SAMPLE_TAG='users', **conf)
    app = add_dataset(host, 'tank/data/shared') / 'app'
    app.mkdir(); (app / 'data').write_text('app frozen bytes')
    users = add_dataset(host, 'tank/data/share/users')
    (users / 'file').write_text('user frozen bytes')
    backups = add_dataset(host, 'tank/data/backups')
    (backups / 'excluded').write_text('must never back up')
    return host


def snapshot_tree(host, name='autosnap_2027-01-15_08:00:00_hourly'):
    for ds in ('tank/data', 'tank/data/shared', 'tank/data/share/users'):
        host.zfs('snapshot', ds+'@'+name)
    return name


@needs_ansible
def test_single_dataset_render_is_byte_identical(tmp_path):
    result, out = ansible_render(tmp_path, full_vault())
    assert result['status'] == 'PASS', result['findings']
    actual = {k:hashlib.sha256(v.encode()).hexdigest() for k,v in rendered(out).items() if not k.startswith('_')}
    expected = json.loads((Path(__file__).parent/'fixtures/skbackup-single-dataset-render.json').read_text())
    assert actual == expected


@needs_ansible
def test_recursive_sanoid_and_options_render(tmp_path):
    vault = full_vault(datasets={'recursive':True,'exclude':['tank/data/backups'],
                               'retention':{'tank/data/share/users':{'hourly':0,'daily':14}}},
                       snapshots={'adopt_existing':True},
                       offsite={'pack_size_mib':64,'compression':'max','read_concurrency':2,'check_subset':'2.5%',
                                'sets':[{'name':'users','kind':'paths','paths':['share/users'],'limit_upload_kbps':1234,'pack_size_mib':64}]},
                       restore_test={'enabled':False})
    result,out = ansible_render(tmp_path,vault)
    assert result['status']=='PASS',result['findings']
    sanoid=(out/'etc/sanoid/sanoid.conf').read_text()
    assert 'recursive = yes' in sanoid
    excluded=sanoid.split('[tank/data/backups]')[1].split('[')[0]
    assert 'autosnap = no' in excluded and 'autoprune = no' in excluded
    child=sanoid.split('[tank/data/share/users]')[1].split('[')[0]
    assert 'hourly = 0' in child and 'daily = 14' in child
    conf=(out/'etc/skbackup/skbackup.conf').read_text()
    assert 'SRC_RECURSIVE=1' in conf and 'RESTIC_PACK_SIZE=64' in conf
    assert 'limit-upload=1234' in (out/'etc/skbackup/restic-sets.conf').read_text()
    assert 'pack-size=64' in (out/'etc/skbackup/restic-sets.conf').read_text()
    assert 'restic check' in (out/'etc/systemd/system/skbackup-repo-check.service').read_text()


@needs_ansible
@pytest.mark.parametrize('dataset',['other/backup','tank/data','tank/data/../outside'])
def test_deploy_rejects_excludes_outside_root(tmp_path,dataset):
    result,_=ansible_render(tmp_path,full_vault(datasets={'recursive':True,'exclude':[dataset]}))
    assert result['status']=='FAIL'


@pytest.mark.parametrize('dataset',['tank/data/users-private','tank/data/recovery-old'])
def test_tree_private_dataset_requires_explicit_opt_in(tmp_path,dataset):
    host=tree_host(tmp_path);add_dataset(host,dataset)
    before=json.loads(host.zstate.read_text())['snaps']
    proc=host.run('sync')
    assert proc.returncode!=0 and 'private' in proc.stderr.lower()
    assert json.loads(host.zstate.read_text())['snaps']==before
    host.conf.write_text(host.conf.read_text()+f'SRC_INCLUDE_PRIVATE={dataset}\n')
    assert host.run('sync').returncode==0


def test_tree_sync_freezes_owning_dataset_only(tmp_path):
    host=tree_host(tmp_path)
    assert host.run('sync',check=True).returncode==0
    assert (host.copies/'app/data').read_text()=='app frozen bytes'
    state=json.loads(host.zstate.read_text())
    created=[x[1] for x in state.get('events',[]) if x[0]=='snapshot' and '@skbackup-' in x[1]]
    assert len(created)==1 and created[0].startswith('tank/data/shared@')
    assert not any('@skbackup-' in s for s in state['snaps'])


@pytest.mark.parametrize('src',['backups','shared/../backups','/etc','shared/link'])
def test_copy_rejects_excluded_traversal_and_symlink_sources(tmp_path,src):
    host=tree_host(tmp_path)
    (host.data/'shared/link').symlink_to(host.data/'backups',target_is_directory=True)
    (host.root/'etc/apps.conf').write_text(f'app {src} 7 4 3\n')
    proc=host.run('sync')
    assert proc.returncode!=0
    assert not (host.state/'last-ok-app').exists()


@needs_restic
def test_tree_backup_mounts_children_and_restores_from_origin(tmp_path):
    host=tree_host(tmp_path);snapshot_tree(host)
    host.run('restic','init',check=True)
    host.run('restic','backup',check=True)
    name=(host.state/'restic-src-users').read_text().strip().split('@')[1]
    (host.data/'share/users/file').write_text('live changed bytes')
    host.run('restic','restore-test',check=True)
    provenance=(host.state/'restic-tree-src-users').read_text()
    assert 'tank/data/share/users@'+name in provenance
    events=json.loads(host.zstate.read_text())['events']
    mounts=[x[1] for x in events if x[0]=='mount']
    unmounts=[x[1] for x in events if x[0]=='umount']
    assert mounts[0]==str(host.mnt) and unmounts==list(reversed(mounts))
    assert not (host.mnt/'share/users/file').exists()


@needs_restic
def test_tree_capture_supersedes_non_atomic_sanoid_names(tmp_path):
    host=tree_host(tmp_path);old=snapshot_tree(host)
    host.zfs('snapshot','tank/data@autosnap_2027-01-16_08:00:00_hourly')
    host.run('restic','init',check=True)
    host.run('restic','backup',check=True)
    first=(host.state/'restic-src-users').read_text().strip()
    assert old not in first and '@skbackup-restic-' in first
    host.zfs('destroy','tank/data/share/users@'+old)
    host.zfs('snapshot','tank/data/share/users@autosnap_2027-01-17_08:00:00_hourly')
    proc=host.run('restic','backup',check=True)
    second=(host.state/'restic-src-users').read_text().strip()
    assert second != first and 'fallback' not in proc.stdout.lower()
    assert len({s.split('@')[1] for s in (host.state/'restic-tree-src-users').read_text().splitlines()})==1


@needs_restic
def test_parent_set_excludes_child_and_failure_unmounts_all(tmp_path):
    host=tree_host(tmp_path);snapshot_tree(host)
    (host.root/'etc/restic-sets.conf').write_text('root paths .\n')
    host.run('restic','init',check=True);host.run('restic','backup',check=True)
    proc=subprocess.run(['restic','ls','latest','--json'],env=dict(host.env(),RESTIC_REPOSITORY=str(host.repo),RESTIC_PASSWORD='test-restic-password'),capture_output=True,text=True,check=True)
    assert '/backups/excluded' not in proc.stdout
    (host.root/'etc/restic-sets.conf').write_text('bad paths backups\n')
    assert host.run('restic','backup').returncode!=0
    state=json.loads(host.zstate.read_text())
    assert not state.get('mounts')


def test_check_reports_every_dataset_and_keeps_existing_snapshots(tmp_path):
    host=tree_host(tmp_path,SNAP_ADOPT_EXISTING=1);snapshot_tree(host)
    host.zfs('snapshot','tank/data/share/users@auto-other-tool')
    host.set_creation('tank/data/share/users@autosnap_2027-01-15_08:00:00_hourly',1)
    proc=host.run('check')
    assert 'STALE tier1: newest tank/data/share/users@' in proc.stdout
    assert 'auto-other-tool' in proc.stdout and 'external' in proc.stdout.lower()
    assert 'auto-other-tool' in host.snaps('tank/data/share/users')


def test_vault_restic_options_and_set_overrides_reach_cli(tmp_path):
    host=tree_host(tmp_path,RESTIC_PACK_SIZE=16,RESTIC_COMPRESSION='max',RESTIC_READ_CONCURRENCY=3,
                   RESTIC_RETRY_LOCK='2m',RESTIC_PRUNE_MAX_UNUSED='7%',RESTIC_CHECK_SUBSET='3%')
    snapshot_tree(host)
    log=host.root/'restic-args.jsonl'
    shim=host.bin/'restic'
    shim.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\np=Path('+repr(str(log))+')\nwith p.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    shim.chmod(0o755)
    (host.root/'etc/restic-sets.conf').write_text('users paths share/users limit-upload=1234 pack-size=64\n')
    host.run('restic','backup',check=True);host.run('restic','forget-prune',check=True);host.run('restic','check',check=True)
    calls=[json.loads(x) for x in log.read_text().splitlines()]
    backup=next(x for x in calls if 'backup' in x)
    assert backup[backup.index('--pack-size')+1]=='64'
    assert backup[backup.index('--limit-upload')+1]=='1234'
    assert backup[backup.index('--compression')+1]=='max'
    assert backup[backup.index('--retry-lock')+1]=='2m'
    assert '--exclude-caches' in backup and '--exclude-if-present=.nobackup' in backup
    assert '--read-concurrency=3' in backup
    prune=next(x for x in calls if 'forget' in x)
    assert prune[prune.index('--max-unused')+1]=='7%'
    assert '--read-data-subset=3%' in next(x for x in calls if 'check' in x)


@pytest.mark.parametrize("query", ["", "?rid=fake-run"])
def test_healthcheck_success_and_failure_hide_url(tmp_path,query):
    import http.server
    import threading
    received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path)
            self.send_response(200);self.end_headers()
        def log_message(self,*args): pass
    server=http.server.HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        env=tmp_path/'ping.env';env.write_text(f'HEALTHCHECKS_URL=http://127.0.0.1:{server.server_port}/opaque-test-token{query}\n')
        host=tree_host(tmp_path,HEALTHCHECKS_ENV_FILE=str(env))
        proc=host.run('sync',check=True)
        (host.root/'etc/apps.conf').write_text('app backups 7 4 3\n')
        failed=host.run('sync')
        assert failed.returncode!=0 and received==['/opaque-test-token'+query,'/opaque-test-token/fail'+query]
        assert 'opaque-test-token' not in proc.stdout+proc.stderr+failed.stdout+failed.stderr
    finally: server.shutdown();server.server_close();worker.join()


@needs_ansible
def test_deploy_private_tree_gate_runs_before_sanoid(tmp_path,monkeypatch):
    import render_playbooks as rp
    monkeypatch.setitem(rp.FAKE_STDOUT,'skb_dataset_tree','tank/data/recovery-old')
    result,_=ansible_render(tmp_path,full_vault(datasets={'recursive':True}))
    assert result['status']=='FAIL' and 'private dataset' in result['log']


@pytest.mark.parametrize('source',['.','shared'])
def test_copy_refuses_source_spanning_nested_dataset(tmp_path,source):
    host=tree_host(tmp_path)
    add_dataset(host,'tank/data/shared/nested')
    (host.root/'etc/apps.conf').write_text(f'app {source} 7 4 3\n')
    proc=host.run('sync')
    assert proc.returncode!=0 and 'child dataset' in proc.stderr
    assert not any('@skbackup-' in s for s in json.loads(host.zstate.read_text())['snaps'])


@needs_restic
def test_partial_mount_failure_unmounts_parent(tmp_path):
    host=tree_host(tmp_path)
    host.sanoid()
    host.zfs('snapshot','tank/data/shared@autosnap_only_shared')
    host.run('restic','init',check=True)
    mount=host.bin/'mount';original=mount.read_text().split('\n',1)[1]
    mount.write_text('#!/bin/sh\nif [ "$6" = "'+str(host.mnt/'shared')+'" ]; then exit 1; fi\n'+original)
    proc=host.run('restic','backup')
    assert proc.returncode!=0
    state=json.loads(host.zstate.read_text())
    mounts=[e[1] for e in state['events'] if e[0]=='mount']
    unmounts=[e[1] for e in state['events'] if e[0]=='umount']
    assert mounts and unmounts==list(reversed(mounts)) and not state['mounts']


def test_explicit_empty_marker_list_is_respected(tmp_path):
    host=tree_host(tmp_path,RESTIC_EXCLUDE_MARKERS='');snapshot_tree(host)
    log=host.root/'args'
    shim=host.bin/'restic';shim.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> '+str(log)+'\n');shim.chmod(0o755)
    host.run('restic','backup',check=True)
    assert '--exclude-if-present=' not in log.read_text()


@needs_ansible
@pytest.mark.parametrize('offsite',[{'pack_size_mib':1},{'compression':'extreme'},
                                    {'read_concurrency':-1},{'retry_lock':'forever'},
                                    {'check_subset':'0%'},{'check_subset':'101%'},
                                    {'exclude_if_present':['../escape']},
                                    {'sets':[{'name':'bad','kind':'paths','paths':['/etc']}]}])
def test_bad_restic_options_fail_render(tmp_path,offsite):
    result,_=ansible_render(tmp_path,full_vault(offsite=offsite,restore_test={'enabled':False}))
    assert result['status']=='FAIL'


@needs_ansible
def test_daily_only_dataset_check_threshold_is_rendered(tmp_path):
    result,out=ansible_render(tmp_path,full_vault(datasets={'recursive':True,'retention':{'tank/data/share/users':{'hourly':0,'daily':14}}}))
    assert result['status']=='PASS',result['findings']
    assert 'tank/data/share/users=1560' in (out/'etc/skbackup/skbackup.conf').read_text()


def test_per_dataset_daily_staleness_policy(tmp_path):
    import time
    host=tree_host(tmp_path,SRC_MAX_AGES='tank/data/share/users=1560');snapshot_tree(host)
    host.set_creation('tank/data/share/users@autosnap_2027-01-15_08:00:00_hourly',int(time.time())-20*3600)
    proc=host.run('check')
    assert 'OK tier1: tank/data/share/users@' in proc.stdout
    host.set_creation('tank/data/share/users@autosnap_2027-01-15_08:00:00_hourly',1)
    assert 'STALE tier1: newest tank/data/share/users@' in host.run('check').stdout


def test_timer_termination_pings_failure_and_unmounts(tmp_path):
    import http.server
    import signal
    import threading
    import time
    from skbackup_support import BACKUP
    received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.path);self.send_response(200);self.end_headers()
        def log_message(self,*args): pass
    server=http.server.HTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    env=tmp_path/'ping.env';env.write_text(f'HEALTHCHECKS_URL=http://127.0.0.1:{server.server_port}/opaque-test-token\n')
    host=tree_host(tmp_path,HEALTHCHECKS_ENV_FILE=str(env));snapshot_tree(host)
    ready=tmp_path/'ready'
    shim=host.bin/'restic'
    shim.write_text('#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nif "backup" in sys.argv:\n Path('+repr(str(ready))+').touch()\n time.sleep(60)\n')
    shim.chmod(0o755)
    proc=subprocess.Popen(['bash',str(BACKUP),'--conf',str(host.conf),'restic','backup'],env=host.env(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        deadline=time.monotonic()+10
        while not ready.exists() and time.monotonic()<deadline: time.sleep(0.05)
        assert ready.exists()
        os.killpg(proc.pid,signal.SIGTERM)
        stdout,stderr=proc.communicate(timeout=10)
        assert proc.returncode!=0 and received==['/opaque-test-token/fail']
        assert not json.loads(host.zstate.read_text())['mounts']
        assert not any('@skbackup-restic-' in s for s in json.loads(host.zstate.read_text())['snaps'])
        assert 'opaque-test-token' not in stdout+stderr
    finally:
        if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        server.shutdown();server.server_close();worker.join()


@needs_restic
def test_child_restore_detects_snapshot_corruption(tmp_path):
    host=tree_host(tmp_path);snapshot_tree(host)
    host.run('restic','init',check=True);host.run('restic','backup',check=True)
    name=(host.state/'restic-src-users').read_text().strip().split('@')[1]
    (host.data/'share/users/.zfs/snapshot'/name/'file').write_text('corrupted frozen bytes')
    proc=host.run('restic','restore-test')
    assert proc.returncode!=0 and 'MISMATCH' in proc.stdout and 'RESULT: FAIL' in proc.stdout


@needs_restic
def test_exclusion_remains_effective_when_dataset_is_absent(tmp_path):
    host=tree_host(tmp_path)
    state=json.loads(host.zstate.read_text());del state['datasets']['tank/data/backups'];host.zstate.write_text(json.dumps(state))
    snapshot_tree(host)
    (host.root/'etc/apps.conf').write_text('app backups 7 4 3\n')
    assert host.run('sync').returncode!=0
    (host.root/'etc/restic-sets.conf').write_text('root paths .\n')
    host.run('restic','init',check=True);host.run('restic','backup',check=True)
    proc=subprocess.run(['restic','ls','latest','--json'],env=dict(host.env(),RESTIC_REPOSITORY=str(host.repo),RESTIC_PASSWORD='test-restic-password'),capture_output=True,text=True,check=True)
    assert '/backups/excluded' not in proc.stdout


@needs_ansible
def test_inactive_tree_policy_cannot_be_silently_ignored(tmp_path):
    result,_=ansible_render(tmp_path,full_vault(datasets={'recursive':False,'exclude':['tank/data/backups']}))
    assert result['status']=='FAIL'
