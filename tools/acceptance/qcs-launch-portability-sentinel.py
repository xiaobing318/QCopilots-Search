#!/usr/bin/env python3
"""Prepare or explicitly launch an isolated ordinary Windows GUI sentinel.

No launch or filesystem writes occur without --execute. Never automates UI,
downloads models, enables Qt test mode, terminates a process, or deletes a run.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True


def windows_root(environment):
    value = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    require(bool(value), "Windows system directory is missing from the environment")
    root = Path(value)
    require(root.is_absolute() and root.is_dir(), "Windows system directory is invalid")
    return root


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def environment(app, qt, mode):
    env = dict(os.environ)
    removed = {key: None for key in env if key.upper().startswith(('QT_', 'QML', 'QCOPILOTS_SEARCH_'))}
    for key in removed:
        env.pop(key, None)
    system = windows_root(env)
    search = [app]
    if mode == 'sdk':
        search.append(qt / 'bin')
    search += [system / 'System32', system]
    env['PATH'] = os.pathsep.join(map(str, search))
    overrides = {**removed, 'QT_QPA_PLATFORM': None, 'QCOPILOTS_SEARCH_TEST_ROOT': None,
                 'QCOPILOTS_SEARCH_KEEP_TEST_ARTIFACTS': None, 'PATH': env['PATH']}
    if mode == 'sdk':
        env['QT_PLUGIN_PATH'] = str(qt / 'plugins')
        overrides['QT_PLUGIN_PATH'] = env['QT_PLUGIN_PATH']
    return env, overrides


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('build-record', 'app-bin', 'qt-prefix', 'run-parent'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--head', required=True)
    parser.add_argument('--source-tree', required=True)
    parser.add_argument('--runtime-mode', choices=('sdk', 'packaged'), default='packaged')
    parser.add_argument('--model-dir', type=Path, help='Optional existing model directory. Default is a fresh empty owned directory.')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    require(re.fullmatch(r'[0-9a-f]{40}', args.head)
            and re.fullmatch(r'[0-9a-f]{40}', args.source_tree),
            'Head and tree must be full lower-case Git identities')
    source, record_path, app, qt, parent = [path.resolve() for path in
        (args.source, args.build_record, args.app_bin, args.qt_prefix, args.run_parent)]
    receipt = json.loads(record_path.read_text(encoding='utf-8-sig'))
    identity = receipt['identity']
    require(identity['candidateHead'] == args.head and identity['checkoutCommit'] == args.head
            and identity['sourceTree'] == args.source_tree and identity['workingTreeModified'] is False,
            'Requested candidate does not match the clean build receipt')
    require(source == Path(receipt['sourceDirectory']).resolve(), 'Source checkout does not match build receipt')
    programs = {name: app / (name + '.exe') for name in
                ('qcs-gui', 'qcs-service', 'qcs-worker', 'qcs-cli')}
    hashes = {name: digest(path) for name, path in programs.items()}
    require(all(hashes[name] == receipt['programs'][name]['sha256'] for name in programs),
            'Selected ordinary application binaries differ from the frozen build receipt')
    plan = {'kind': 'qcopilots-search-sentinel-launch-plan', 'executeRequested': args.execute,
            'head': args.head, 'sourceTree': args.source_tree, 'buildRecord': str(record_path),
            'buildRecordSha256': digest(record_path), 'applicationDirectory': str(app),
            'runtimeMode': args.runtime_mode, 'qtPrefix': str(qt), 'runParent': str(parent),
            'uniqueRootAtExecution': 'qcs-sent-<random>',
            'modelDirectory': str(args.model_dir.resolve()) if args.model_dir else 'fresh empty directory under unique root',
            'programSha256': hashes, 'testMode': False, 'offscreen': False,
            'uiAutomation': False, 'cleanup': 'Owner closes observed GUI through Computer Use. No script kill/delete.'}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    require(os.name == 'nt', 'This sentinel launcher is Windows-specific')
    require(parent.is_dir() and not parent.is_relative_to(source), 'Existing external run parent is required')
    require(qt.is_dir(), 'Qt prefix is missing')
    evidence = module(source / 'tools/qcs-release-evidence.py', 'qcopilots_sentinel_evidence')
    require(evidence.source_identity(source, args.head) == identity, 'Current source identity differs from candidate')
    require(not evidence.verified_build(record_path.parent, receipt), 'Build receipt integrity check failed')
    helper_path = Path(__file__).with_name('qcs-portability-sentinel.py')
    observer_module = module(helper_path, 'qcopilots_sentinel_observer')
    pwsh = shutil.which('pwsh')
    require(pwsh is not None, 'PowerShell 7 is required for process identity observation')
    root = Path(tempfile.mkdtemp(prefix='qcs-sent-', dir=parent))
    profile, documents, caller, logs = [root / name for name in ('profile', 'documents', 'caller', 'logs')]
    models = args.model_dir.resolve() if args.model_dir else root / 'models'
    for directory in (profile, documents, caller, logs):
        directory.mkdir()
    if args.model_dir:
        require(models.is_dir(), 'Explicit model directory is missing')
    else:
        models.mkdir()
    fixture = documents / '独立哨兵 ordinary sentinel.txt'
    fixture.write_text('QCopilots Search portability sentinel.\n独立窗口的原始文件。\nExact token sentinelorchidharbour.\n', encoding='utf-8')
    env, overrides = environment(app, qt, args.runtime_mode)
    actions, children = [], {}
    write(root / 'launch-plan.json', plan)
    shutil.copyfile(__file__, root / 'launcher.py')
    shutil.copyfile(helper_path, root / 'observer.py')
    shutil.copyfile(record_path, root / 'producer-build-record.json')
    snapshot_script = root / 'observe-owned-processes.ps1'
    snapshot_script.write_text(observer_module.PROCESS_SCRIPT, encoding='utf-8')
    common = ['--profile-dir', str(profile), '--model-dir', str(models)]
    cli_base = [str(programs['qcs-cli']), *common, '--no-start']

    def cli(label, arguments, milliseconds=10000, allow_failure=False):
        command = [*cli_base, '--timeout', str(milliseconds), *arguments]
        result = subprocess.run(command, cwd=caller, env=env, capture_output=True,
                                timeout=milliseconds / 1000 + 10, creationflags=subprocess.CREATE_NO_WINDOW)
        (logs / (label + '.stdout.json')).write_bytes(result.stdout)
        (logs / (label + '.stderr.log')).write_bytes(result.stderr)
        actions.append({'label': label, 'at': utc(), 'argv': command, 'exitCode': result.returncode})
        write(root / 'preparation-actions.json', actions)
        reply = json.loads(result.stdout.decode('utf-8-sig')) if result.stdout.strip() else {}
        if not allow_failure:
            require(result.returncode == 0 and reply.get('ok') is True, 'CLI operation failed: ' + label)
        return result, reply

    def start(name, arguments, visible):
        stdout = (logs / (name + '.stdout.log')).open('wb')
        stderr = (logs / (name + '.stderr.log')).open('wb')
        command = [str(programs[name]), *common, *arguments]
        try:
            process = subprocess.Popen(command, cwd=caller, env=env, stdout=stdout, stderr=stderr,
                                       creationflags=0 if visible else subprocess.CREATE_NO_WINDOW)
        finally:
            stdout.close()
            stderr.close()
        children[name] = process
        actions.append({'label': name, 'at': utc(), 'argv': command, 'pid': process.pid})
        write(root / 'preparation-actions.json', actions)
        return process

    try:
        service = start('qcs-service', [], False)
        ready = False
        for attempt in range(12):
            require(service.poll() is None, 'Owned service exited before readiness')
            result, reply = cli('service-ready-' + str(attempt), ['status'], 2000, True)
            if result.returncode == 0 and reply.get('ok') is True:
                ready = True
                break
            time.sleep(.25)
        require(ready, 'Owned service did not become ready')
        changes = {'roots': [str(documents)], 'excludes': [], 'includeCode': False, 'language': 'zh_CN',
                   'startAtLogin': False, 'minimizeToTray': False, 'checkUpdates': False, 'indexingPaused': False}
        settings_file = root / 'configuration-request.json'
        write(settings_file, changes)
        cli('configure-owned-profile', ['config', 'set', '--file', str(settings_file)])
        cli('index-owned-fixture', ['index', '--wait'], 120000)
        cli('pause-owned-index', ['pause'])
        gui = start('qcs-gui', ['--no-tray', '--no-onboarding'], True)
        rows = None
        for attempt in range(15):
            require(gui.poll() is None and service.poll() is None, 'Owned GUI/service exited during launch')
            result = subprocess.run([pwsh, '-NoProfile', '-File', str(snapshot_script), '-GuiId', str(gui.pid),
                                     '-ServiceId', str(service.pid)], capture_output=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            (logs / f'process-observation-{attempt}.stdout.json').write_bytes(result.stdout)
            (logs / f'process-observation-{attempt}.stderr.log').write_bytes(result.stderr)
            require(result.returncode == 0, 'Cannot observe actual GUI/service identity')
            rows = {int(item['pid']): item for item in json.loads(result.stdout.decode('utf-8-sig'))}
            if int(rows[gui.pid]['mainWindowHandle']) and rows[gui.pid]['responding'] is True:
                break
            time.sleep(.5)
        require(rows and int(rows[gui.pid]['mainWindowHandle']) and rows[gui.pid]['responding'] is True,
                'Ordinary GUI did not expose a responding window')
        record = {'schemaVersion': 1, 'kind': 'qcopilots-search-ordinary-gui-sentinel-launch', 'head': args.head,
                  'sourceTree': args.source_tree, 'identity': identity, 'startedAt': utc(), 'testMode': False,
                  'environmentOverrides': overrides, 'profileDir': str(profile), 'modelDir': str(models),
                  'applicationDirectory': str(app), 'runtimeMode': args.runtime_mode,
                  'processes': {'gui': rows[gui.pid], 'service': rows[service.pid]},
                  'fixtureFiles': [{'path': str(fixture), 'bytes': fixture.stat().st_size, 'sha256': digest(fixture)}],
                  'buildRecord': str(record_path), 'buildRecordSha256': digest(record_path), 'programSha256': hashes,
                  'launcherSha256': digest(__file__), 'observerSha256': digest(helper_path),
                  'preparationComplete': False,
                  'preparationNotes': ['Ordinary production QApplication. No QStandardPaths test mode.',
                                       'No GUI automation. Owner must observe the actual window before and after portability tests.',
                                       'No models downloaded or copied. An empty explicit directory selects keyword mode.',
                                       'No automatic process termination or report deletion. Close this unique GUI through Computer Use.']}
        launch_record = root / 'launch-record.json'
        write(launch_record, record)
        observer = observer_module.SentinelObserver(launch_record, root / 'readiness-observations', args.head, hashes, qt)
        try:
            healthy = False
            last_health_error = ''
            for attempt in range(20):
                try:
                    observer.status('settle-' + str(attempt))
                    healthy = True
                    break
                except RuntimeError as error:
                    last_health_error = str(error)
                    time.sleep(.5)
            require(healthy, 'Sentinel did not reach healthy paused state: ' + last_health_error)
            observer.begin()
        finally:
            for handle in observer.handles.values():
                observer.k.CloseHandle(handle)
            observer.handles.clear()
        require(digest(record_path) == plan['buildRecordSha256']
                and all(digest(path) == hashes[name] for name, path in programs.items()), 'Candidate bytes changed during launch')
        record['preparationComplete'] = True
        record['preparedAt'] = utc()
        write(launch_record, record)
        print(json.dumps({'prepared': True, 'launchRecord': str(launch_record),
                          'guiPid': gui.pid, 'servicePid': service.pid,
                          'next': 'Computer Use observes this actual window. Pass --sentinel-launch-record to portability runner.'},
                         ensure_ascii=False, indent=2))
    except BaseException as error:
        failure = {'prepared': False, 'at': utc(), 'error': type(error).__name__ + ': ' + str(error),
                   'ownedProcesses': {name: {'pid': child.pid, 'exitCode': child.poll()} for name, child in children.items()},
                   'profileDir': str(profile), 'modelDir': str(models), 'applicationDirectory': str(app),
                   'recovery': 'No automatic kill. Owner observes/closes the owned GUI, then uses CLI --no-start shutdown only for this profile if needed.'}
        write(root / 'launch-failure.json', failure)
        print(json.dumps({'failureReport': str(root / 'launch-failure.json'), **failure}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise


if __name__ == '__main__':
    for output in (sys.stdout, sys.stderr):
        if hasattr(output, 'reconfigure'):
            output.reconfigure(encoding='utf-8', errors='backslashreplace')
    main()
