"""Optional notifier example, with stub alert delivery and native ITIL APIs."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

EXAMPLE = Path(__file__).parents[1]/'ansible/optional/skbackup/examples/skbackup-notify.py'


@pytest.fixture
def notifier(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('backup_notify_example', EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    incidents, alerts = [], []
    class Manager:
        def __init__(self, home):
            assert home == tmp_path/'itil'
        def list_incidents(self, service=None):
            return [i for i in incidents if service in i.affected_services]
        def create_incident(self, **kwargs):
            incidents.append(SimpleNamespace(status=SimpleNamespace(value='detected'), **kwargs))
    package = ModuleType('skcapstone'); api = ModuleType('skcapstone.itil'); api.ITILManager = Manager
    monkeypatch.setitem(sys.modules, 'skcapstone', package)
    monkeypatch.setitem(sys.modules, 'skcapstone.itil', api)
    def deliver(argv, **kwargs):
        assert kwargs['timeout'] == 30 and kwargs['check']
        assert kwargs.get('shell', False) is False
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
        alerts.append(argv)
    monkeypatch.setattr(module.subprocess, 'run', deliver)
    for key, value in {'BK_LEVEL':'warn','BK_KEY':'tier1-users','BK_SUBJECT':'Backup warning; $(touch nope)',
                       'BK_BODY':'snapshot stale\nsecond line'}.items():
        monkeypatch.setenv(key, value)
    args = ['--host','storage-a','--agent','backup-operator','--sk-alert','/opt/alerts/bin/sk-alert',
            '--itil-home',str(tmp_path/'itil'),'--lock-file',str(tmp_path/'notify.lock')]
    return SimpleNamespace(module=module, incidents=incidents, alerts=alerts, args=args, api=api)


@pytest.mark.parametrize('level,severity', [('info', None), ('warn','sev3'), ('crit','sev2')])
def test_levels_argv_and_native_incident_mapping(notifier, monkeypatch, level, severity):
    monkeypatch.setenv('BK_LEVEL',level)
    assert notifier.module.main(notifier.args) == 0
    assert notifier.alerts == [['/opt/alerts/bin/sk-alert','-l',level,'-k','skbackup:storage-a:tier1-users',
                                '-t','3600','Backup warning; $(touch nope)\nsnapshot stale\nsecond line']]
    assert len(notifier.incidents) == (severity is not None)
    if severity:
        incident = notifier.incidents[0]
        assert incident.severity == severity and incident.source == 'skbackup'
        assert incident.affected_services == ['skbackup:storage-a']
        assert incident.managed_by == 'backup-operator' and 'backup-key:tier1-users' in incident.tags
        assert incident.impact == 'snapshot stale\nsecond line'


@pytest.mark.parametrize('status', ['resolved', 'closed'])
def test_reuses_active_incident_and_rearms_after_resolution(notifier, status):
    notifier.module.main(notifier.args); notifier.module.main(notifier.args)
    assert len(notifier.incidents) == 1
    notifier.incidents[0].status.value = status
    notifier.module.main(notifier.args)
    assert len(notifier.incidents) == 2


def test_distinct_hosts_and_findings_have_distinct_identity(notifier, monkeypatch):
    notifier.module.main(notifier.args)
    monkeypatch.setenv('BK_KEY','tier4-users')
    notifier.module.main(notifier.args)
    args = list(notifier.args); args[args.index('storage-a')] = 'storage-b'
    notifier.module.main(args)
    assert len(notifier.incidents) == 3
    assert len({a[a.index('-k')+1] for a in notifier.alerts}) == 3


def test_delivery_failure_still_records_incident_without_logging_backend_error(notifier, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr='opaque-test-credential')
    monkeypatch.setattr(notifier.module.subprocess,'run',fail)
    assert notifier.module.main(notifier.args) == 1 and len(notifier.incidents) == 1
    text = capsys.readouterr().err
    assert 'sk-alert delivery failed' in text and 'opaque-test-credential' not in text


def test_itil_failure_still_delivers_alert_and_returns_failure(notifier, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError('opaque-test-credential')
    monkeypatch.setattr(notifier.api.ITILManager,'list_incidents',fail)
    assert notifier.module.main(notifier.args) == 1 and len(notifier.alerts) == 1
    text = capsys.readouterr().err
    assert 'ITIL recording failed' in text and 'opaque-test-credential' not in text


@pytest.mark.parametrize('key,value', [('BK_LEVEL','bogus'), ('BK_KEY',''), ('BK_SUBJECT','')])
def test_missing_or_invalid_context_fails_before_delivery(notifier, monkeypatch, key, value):
    monkeypatch.setenv(key,value)
    assert notifier.module.main(notifier.args) == 2
    assert not notifier.alerts and not notifier.incidents


def test_relative_cli_path_fails_before_delivery(notifier):
    args = list(notifier.args); args[args.index('/opt/alerts/bin/sk-alert')] = 'sk-alert'
    with pytest.raises(SystemExit):
        notifier.module.main(args)
    assert not notifier.alerts and not notifier.incidents


def test_alerting_host_arguments_override_environment(notifier, monkeypatch):
    monkeypatch.setenv('BK_LEVEL','bogus')
    args = notifier.args+['--level','warn','--key','backup-check','--subject','Remote check\nSTALE tier1',
                         '--body','read-only SSH check']
    assert notifier.module.main(args) == 0
    assert notifier.alerts[0][-1] == 'Remote check\nSTALE tier1\nread-only SSH check'
    assert notifier.alerts[0][notifier.alerts[0].index('-k')+1] == 'skbackup:storage-a:backup-check'


def test_engine_command_notifier_supplies_example_context(tmp_path):
    import json
    from skbackup_support import FakeHost
    package = tmp_path/'stubs/skcapstone'; package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    (package/'itil.py').write_text('''import json
class ITILManager:
 def __init__(self, home): self.home=home; home.mkdir(exist_ok=True)
 def list_incidents(self, service=None): return []
 def create_incident(self, **kwargs):
  with (self.home/'records').open('a') as f: f.write(json.dumps(kwargs)+'\\n')
''')
    record = tmp_path/'argv'
    alert = tmp_path/'sk-alert'
    alert.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\n'
                     'with Path('+repr(str(record))+').open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    alert.chmod(0o755)
    command = (f'{sys.executable} {EXAMPLE} --host storage-a --agent backup-operator --sk-alert {alert} '
               f'--itil-home {tmp_path}/itil --lock-file {tmp_path}/notify.lock')
    host = FakeHost(tmp_path/'host', NOTIFY_MODE='command', NOTIFY_COMMAND=command).seed()
    proc = host.run('check', '--notify', env={'PYTHONPATH':str(tmp_path/'stubs')})
    assert proc.returncode == 1 and 'notifier failed' not in proc.stdout+proc.stderr
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    assert calls and all(call[call.index('-k')+1].startswith('skbackup:storage-a:') for call in calls)
    assert all('\n' in call[-1] and '[SKBackup]' in call[-1] for call in calls)
    incidents = [json.loads(line) for line in (tmp_path/'itil/records').read_text().splitlines()]
    assert incidents and all(i['affected_services']==['skbackup:storage-a'] for i in incidents)
