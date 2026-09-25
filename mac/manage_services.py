#!/usr/bin/env python3
"""Install/status/stop two independent macOS LaunchAgents."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

import karing_mac as km

JOBS = {'gc': ['connection_gc.py', '--apply', '--loop'], 'watch': ['tunnel_watch.py']}
AGENTS = Path.home() / 'Library/LaunchAgents'


def service(name):
    return f'gui/{os.getuid()}/com.karing.net.{name}'


def plist_data(name):
    python = km.ROOT / '.vfox/sdks/python/bin/python3'
    subprocess.run([str(python), '-c', 'import sys; assert sys.version_info >= (3, 12)'], check=True, timeout=10)
    script, *args = JOBS[name]
    return {'Label': f'com.karing.net.{name}', 'ProgramArguments': [str(python), str(km.ROOT / script), *args],
            'WorkingDirectory': str(km.ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
            'ProcessType': 'Background', 'ExitTimeOut': 15,
            'EnvironmentVariables': {'KARING_DATA_DIR': str(km.KARING_DIR), 'KARING_STATE_DIR': str(km.STATE_DIR),
                                     'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'stop', 'status'])
    args = parser.parse_args()
    if args.command == 'install':
        AGENTS.mkdir(exist_ok=True, parents=True)
        km.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in JOBS:
        path = AGENTS / f'com.karing.net.{name}.plist'
        if args.command == 'status':
            result = subprocess.run(['/bin/launchctl', 'print', service(name)], capture_output=True, text=True)
            print(name, result.stdout if result.returncode == 0 else result.stderr.strip())
        elif args.command == 'stop':
            result = subprocess.run(['/bin/launchctl', 'bootout', service(name)], capture_output=True, text=True)
            if result.returncode and 'Could not find service' not in result.stderr and 'No such process' not in result.stderr:
                raise RuntimeError(result.stderr)
            print(name, 'stopped')
        else:
            data = plist_data(name)
            status = subprocess.run(['/bin/launchctl', 'print', service(name)], capture_output=True)
            if status.returncode == 0:
                subprocess.run(['/bin/launchctl', 'bootout', service(name)], check=True, capture_output=True)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    probe = subprocess.run(['/bin/launchctl', 'print', service(name)], capture_output=True)
                    if probe.returncode:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError(f'{name}: old launchd job did not unload')
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(plistlib.dumps(data))
            tmp.chmod(0o600)
            tmp.replace(path)
            subprocess.run(['/usr/bin/plutil', '-lint', str(path)], check=True)
            subprocess.run(['/bin/launchctl', 'enable', service(name)], check=True)
            # bootout can return before launchd has released the old job label.
            for attempt in range(8):
                result = subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)],
                                        capture_output=True, text=True)
                if result.returncode == 0:
                    break
                if attempt == 7:
                    raise RuntimeError(result.stderr.strip())
                time.sleep(1)
            print(name, 'installed')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
