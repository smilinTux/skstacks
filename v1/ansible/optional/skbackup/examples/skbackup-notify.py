#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in command notifier example. Not installed by the deploy playbooks."""
import argparse
import fcntl
import os
import subprocess
import sys
from pathlib import Path


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError('an absolute path is required')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Storage host identity, even on an alerting host')
    parser.add_argument('--agent', required=True, help='Native ITIL managing agent')
    parser.add_argument('--sk-alert', required=True, type=absolute_path)
    parser.add_argument('--itil-home', required=True, type=absolute_path)
    parser.add_argument('--lock-file', required=True, type=absolute_path)
    # Explicit arguments also support alert-host-check's positional contract.
    for field in ('level', 'key', 'subject', 'body'):
        parser.add_argument('--'+field, default=os.environ.get('BK_'+field.upper(), ''))
    args = parser.parse_args(argv)
    level, key, subject, body = args.level, args.key, args.subject, args.body
    if level not in ('info', 'warn', 'crit') or not key or not subject:
        print('invalid backup notification context', file=sys.stderr)
        return 2
    service = f'skbackup:{args.host}'
    failed = False
    try:
        subprocess.run([str(args.sk_alert), '-l', level, '-k', f'{service}:{key}', '-t', '3600',
                        subject+'\n'+body], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        print('sk-alert delivery failed', file=sys.stderr)
        failed = True
    if level != 'info':
        try:
            from skcapstone.itil import ITILManager
            tag = f'backup-key:{key}'
            args.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(os.open(args.lock_file, os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                manager = ITILManager(args.itil_home)
                active = any(tag in incident.tags and incident.status.value not in ('resolved', 'closed')
                             for incident in manager.list_incidents(service=service))
                if not active:
                    manager.create_incident(title=subject, severity={'warn':'sev3', 'crit':'sev2'}[level],
                                            source='skbackup',
                                            affected_services=[service], impact=body,
                                            managed_by=args.agent, created_by=args.agent, tags=['skbackup', tag])
        except Exception:
            print('ITIL recording failed', file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
