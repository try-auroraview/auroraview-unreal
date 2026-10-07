#!/usr/bin/env python3
"""Cook, stage, package and run an isolated Win64 Game with external Python tools.

A BuildPlugin receipt is an input, never proof of packaged-game acceptance.
This entry point requires real BuildCookRun products and an authenticated live
native host, then requests normal shutdown and records its process exit.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

import build_plugin
import preflight_engine


ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'AuroraViewGameFixture'


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def inventory(root):
    if not root.is_dir() or root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise build_plugin.BuildError('Validation root must be a real directory: ' + str(root))
    files = {}
    for path in sorted(root.rglob('*')):
        attributes = path.lstat()
        if path.is_symlink() or getattr(attributes, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise build_plugin.BuildError('Links are not allowed in validation inputs: ' + str(path))
        if not build_plugin.inside(path.resolve(), root.resolve()):
            raise build_plugin.BuildError('Path escapes the validation root: ' + str(path))
        if path.is_file():
            files[path.relative_to(root).as_posix()] = build_plugin.sha256(path)
    return files


def verify_package(package, expected):
    if not isinstance(expected, dict) or not expected:
        raise build_plugin.BuildError('Build receipt has no package hashes')
    for name, digest in expected.items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or any(part in ['', '.'] or part.rstrip('. ') != part for part in name.split('/'))
                or not isinstance(digest, str) or len(digest) != 64):
            raise build_plugin.BuildError('Unsafe package inventory entry: ' + name)
    if inventory(package) != expected:
        raise build_plugin.BuildError('Package files differ from the passing build receipt')


def verify_inputs(engine, package):
    receipt_path = package.parent / 'build-receipt.json'
    receipt = build_plugin.read_json(receipt_path)
    if (receipt.get('status') != 'pass' or receipt.get('unreal_compile') != 'pass'
            or Path(receipt.get('package', {}).get('root', '')).resolve() != package):
        raise build_plugin.BuildError('A passing BuildPlugin receipt for this exact package is required')
    version_path = engine / 'Engine/Build/Build.version'
    version = build_plugin.read_json(version_path)
    policy = preflight_engine.engine_policy(version)
    modules_path = engine / f'Engine/Binaries/Win64/{policy["editor_target"]}.modules'
    modules = build_plugin.read_json(modules_path)
    identity = receipt.get('engine', {})
    if (identity.get('build_id') != modules.get('BuildId')
            or identity.get('version_sha256') != build_plugin.sha256(version_path)
            or identity.get('modules_sha256') != build_plugin.sha256(modules_path)):
        raise build_plugin.BuildError('Build receipt identifies a different installed engine')
    if build_plugin.git_identity(ROOT) != receipt.get('source'):
        raise build_plugin.BuildError('Source differs from the package build inputs')
    verify_package(package, receipt['package']['files_sha256'])
    build_plugin.validate_descriptor(package / 'AuroraView.uplugin')
    return receipt_path, receipt, policy


def create_project(root, package, version):
    source = root / 'Source' / PROJECT
    source.mkdir(parents=True)
    plugins = root / 'Plugins'
    plugins.mkdir()
    shutil.copytree(package, plugins / 'AuroraView')
    write_json(root / f'{PROJECT}.uproject', {
        'FileVersion': 3, 'EngineAssociation': version,
        'Description': 'Disposable AuroraView packaged Game acceptance fixture',
        'Modules': [{'Name': PROJECT, 'Type': 'Runtime', 'LoadingPhase': 'Default'}],
        'Plugins': [{'Name': 'AuroraView', 'Enabled': True}],
    })
    (root / 'Source' / f'{PROJECT}.Target.cs').write_text(
        'using UnrealBuildTool;\n'
        f'public class {PROJECT}Target : TargetRules {{\n'
        f'    public {PROJECT}Target(TargetInfo Target) : base(Target) {{\n'
        '        Type = TargetType.Game;\n'
        f'        ExtraModuleNames.Add("{PROJECT}");\n'
        '    }\n}\n', encoding='utf-8')
    (root / 'Source' / f'{PROJECT}Editor.Target.cs').write_text(
        'using UnrealBuildTool;\n'
        f'public class {PROJECT}EditorTarget : TargetRules {{\n'
        f'    public {PROJECT}EditorTarget(TargetInfo Target) : base(Target) {{\n'
        '        Type = TargetType.Editor;\n'
        f'        ExtraModuleNames.Add("{PROJECT}");\n'
        '    }\n}\n', encoding='utf-8')
    (source / f'{PROJECT}.Build.cs').write_text(
        'using UnrealBuildTool;\n'
        f'public class {PROJECT} : ModuleRules {{\n'
        f'    public {PROJECT}(ReadOnlyTargetRules Target) : base(Target) {{\n'
        '        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;\n'
        '        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine", "AuroraViewRuntime" });\n'
        '    }\n}\n', encoding='utf-8')
    (source / f'{PROJECT}.cpp').write_text(
        '#include "Modules/ModuleManager.h"\n'
        f'IMPLEMENT_PRIMARY_GAME_MODULE(FDefaultGameModuleImpl, {PROJECT}, "{PROJECT}");\n', encoding='utf-8')
    config = root / 'Config'
    config.mkdir()
    (config / 'DefaultEngine.ini').write_text(
        '[/Script/EngineSettings.GameMapsSettings]\n'
        'GameDefaultMap=/Engine/Maps/Entry\n'
        '[/Script/Engine.RendererSettings]\n'
        'r.DefaultFeature.AutoExposure=False\n', encoding='utf-8')


def stop_owned(process):
    if process.poll() is None:
        subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/taskkill.exe'),
                        '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        process.wait(timeout=15)


def run_logged(command, working, log, environment, timeout):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process_receipt = {'started_utc': now(), 'command': command, 'pid': None,
                       'exit_code': None, 'timed_out': False}
    path = log.with_suffix('.process.json')
    with log.open('wb') as stream:
        process = subprocess.Popen(command, cwd=working, env=environment, stdout=stream,
                                   stderr=subprocess.STDOUT, startupinfo=startup)
        process_receipt['pid'] = process.pid
        write_json(path, process_receipt)
        try:
            process_receipt['exit_code'] = process.wait(timeout=timeout)
            if process.returncode:
                raise build_plugin.BuildError(f'BuildCookRun exited {process.returncode}; see {log}')
        except subprocess.TimeoutExpired as error:
            process_receipt['timed_out'] = True
            raise build_plugin.BuildError(f'BuildCookRun exceeded {timeout} seconds') from error
        finally:
            stop_owned(process)
            process_receipt.update(exit_code=process.returncode, completed_utc=now())
            write_json(path, process_receipt)


def game_command(engine, project, archive, policy):
    command = [str(engine / 'Engine/Build/BatchFiles/RunUAT.bat'), 'BuildCookRun',
               '-project=' + str(project), '-target=' + PROJECT, '-noP4', '-platform=Win64',
               '-clientconfig=Development', '-build', '-cook', '-stage', '-pak', '-package',
               '-archive', '-archivedirectory=' + str(archive), '-map=/Engine/Maps/Entry',
               '-unattended', '-utf8output']
    if policy['version'] == '4.26':
        command.append('-VS2019')
    if policy['version'].startswith('4.') and (engine / 'Engine/Binaries/DotNET/AutomationTool.exe').is_file():
        command.append('-nocompile')
    return command


def validate_executable(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise build_plugin.BuildError('Game output is not a native executable')
        stream.seek(struct.unpack_from('<I', header, 60)[0])
        pe = stream.read(26)
        if (len(pe) != 26 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664
                or struct.unpack_from('<H', pe, 22)[0] & 0x2000
                or struct.unpack_from('<H', pe, 24)[0] != 0x20B):
            raise build_plugin.BuildError('Game output is not a Win64 PE32+ executable')


def stage_evidence(project, archive, engine, package_receipt):
    files = inventory(archive)
    if not files:
        raise build_plugin.BuildError('BuildCookRun produced no archived Game files')
    for name in files:
        if Path(name).suffix.lower() in ['.dll', '.exe', '.lib', '.obj'] and any(
                forbidden in Path(name).name.lower() for forbidden in ['auroravieweditor', 'unrealeditor', 'ue4editor']):
            raise build_plugin.BuildError('Editor binary was staged into the Game: ' + name)
    candidates = [archive / name for name in files if '/Binaries/Win64/' in '/' + name
                  and Path(name).name in [PROJECT + '.exe', PROJECT + '-Win64-Development.exe']]
    if len(candidates) != 1:
        raise build_plugin.BuildError('Expected one actual Game executable under Binaries/Win64')
    executable = candidates[0]
    validate_executable(executable)
    target_paths = list((project / 'Binaries/Win64').glob('*.target'))
    targets = []
    for path in target_paths:
        target = build_plugin.read_json(path)
        if (target.get('TargetName') == PROJECT and target.get('Platform') == 'Win64'
                and target.get('Configuration') == 'Development'):
            targets.append((path, target))
    if len(targets) != 1:
        raise build_plugin.BuildError('Missing unique actual Development Win64 Game target receipt')
    target_path, target = targets[0]
    if target.get('TargetType', 'Game') != 'Game':
        raise build_plugin.BuildError('Compiled target is not a Game target')
    built_exes = list((project / 'Binaries/Win64').glob(PROJECT + '*.exe'))
    digest = build_plugin.sha256(executable)
    if not any(build_plugin.sha256(path) == digest for path in built_exes):
        raise build_plugin.BuildError('Staged executable differs from the actual compiled Game')
    resources = {}
    for relative, expected in package_receipt['source_assets_sha256'].items():
        if not relative.startswith(('Resources/', 'ThirdParty/AuroraViewCore/')):
            continue
        matches = [(name, sha) for name, sha in files.items()
                   if name.endswith('/Plugins/AuroraView/' + relative)]
        if len(matches) != 1 or matches[0][1] != expected:
            raise build_plugin.BuildError('Required packaged Runtime resource missing or changed: ' + relative)
        resources[matches[0][0]] = expected
    cef = {name: sha for name, sha in files.items() if '/CEF3/' in '/' + name}
    for required in ['libcef.dll', 'icudtl.dat', 'resources.pak']:
        matches = [(name, sha) for name, sha in cef.items() if Path(name).name == required]
        installed = list((engine / 'Engine/Binaries/ThirdParty/CEF3/Win64').rglob(required))
        if len(matches) != 1 or not any(build_plugin.sha256(path) == matches[0][1] for path in installed):
            raise build_plugin.BuildError('Staged CEF runtime is missing or differs from the installed engine: ' + required)
    return executable, {'files_sha256': files, 'runtime_resources_sha256': resources,
                        'cef_sha256': cef, 'executable_sha256': digest,
                        'target_receipt': str(target_path), 'target_receipt_sha256': build_plugin.sha256(target_path),
                        'target': target, 'editor_binaries': 'absent', 'runtime_linkage': 'native Game target'}


def browser_fixture_html():
    # ES5 syntax and native Promises work with the oldest matrix CEF (Chrome 59).
    # The pinned early stub cannot complete whenReady until Core replaces it.
    return '''<h1>AuroraView packaged browser acceptance</h1><output id="result">Waiting for Core</output>
<script>(function () {
  function start() {
    var bridge = window.auroraview;
    if (!bridge || bridge._isStub || typeof bridge.whenReady !== 'function') {
      window.setTimeout(start, 10);
      return;
    }
    bridge.whenReady().then(function (core) {
      core.on('acceptance:browser-inbound', function (data) {
        core.send_event('acceptance:browser-outbound', {
          nonce: data.nonce, value: data.value, via: 'cef-core', false_value: data.false_value
        });
      });
      return core.call('python.acceptance.echo', {value: 42}).then(function (called) {
        return core.invoke('python.acceptance.echo', {value: 43}).then(function (invoked) {
          document.getElementById('result').textContent = String(called.echo) + ' / ' + String(invoked.echo);
          core.send_event('acceptance:browser-ready', {call: called.echo, invoke: invoked.echo, via: 'cef-core'});
        });
      });
    }).catch(function (error) {
      bridge.send_event('acceptance:browser-error', {message: String(error)});
    });
  }
  start();
}());</script>'''


def validate_browser(client, process, timeout):
    view = 'AuroraViewPackagedAcceptance'
    ready, echoed, errors = [], [], []
    ready_signal, echo_signal = threading.Event(), threading.Event()
    def on_ready(data):
        ready.append(data)
        ready_signal.set()
    def on_echo(data):
        echoed.append(data)
        echo_signal.set()
    def on_error(data):
        errors.append(data)
        ready_signal.set()
        echo_signal.set()
    subscriptions = [client.on('acceptance:browser-ready', on_ready),
                     client.on('acceptance:browser-outbound', on_echo),
                     client.on('acceptance:browser-error', on_error)]
    opened, removed = False, False
    try:
        if client.call('auroraview.view.open', {
                'id': view, 'html': browser_fixture_html(), 'title': 'AuroraView Runtime acceptance'}) is not True:
            raise build_plugin.BuildError('Rendered Game browser did not open')
        opened = True
        deadline = time.monotonic() + min(timeout, 120)
        state = None
        while not ready_signal.wait(0.1):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise build_plugin.BuildError('Rendered browser did not complete Core whenReady/call/invoke')
        if errors or ready != [{'call': 42, 'invoke': 43, 'via': 'cef-core'}]:
            raise build_plugin.BuildError('Rendered Core call/invoke readback failed: ' + repr(errors or ready))
        while True:
            state = client.call('auroraview.view.describe', {'id': view})
            if state.get('ready') is True and isinstance(state.get('generation'), (int, float)) and state['generation'] > 0:
                break
            if process.poll() is not None or time.monotonic() >= deadline:
                raise build_plugin.BuildError('Rendered view never reported ready with a live generation')
            time.sleep(0.1)
        challenge = {'nonce': secrets.token_hex(16), 'value': 44, 'false_value': False}
        client.emit('acceptance:browser-inbound', challenge)
        if not echo_signal.wait(min(timeout, 30)) or errors or echoed != [dict(challenge, via='cef-core')]:
            raise build_plugin.BuildError('Python to rendered Core event to Python readback failed: ' + repr(errors or echoed))
        if client.call('auroraview.view.close', {'id': view}) is not True:
            raise build_plugin.BuildError('Rendered view did not acknowledge close')
        closed = client.call('auroraview.view.describe', {'id': view})
        if closed.get('ready') is not False or closed.get('generation') != 0:
            raise build_plugin.BuildError('Rendered view remained live after close')
        if client.call('auroraview.view.remove', {'id': view}) is not True:
            raise build_plugin.BuildError('Rendered view did not acknowledge removal')
        removed = True
        return {'status': 'pass', 'view': view, 'ready': state, 'core_call': ready[0]['call'],
                'core_invoke': ready[0]['invoke'], 'python_browser_event': echoed[0],
                'closed': closed, 'removed': True}
    finally:
        for unsubscribe in subscriptions:
            unsubscribe()
        if opened and not removed:
            try:
                client.call('auroraview.view.remove', {'id': view})
            except Exception:
                pass  # The outer validator still records the original failed gate.


def run_game(executable, evidence, engine_version, timeout, rendered_browser=False):
    sys.path.insert(0, str(ROOT / 'python'))
    from auroraview_unreal import Client, ProtocolError, RemoteError
    token = secrets.token_urlsafe(32)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    game_log = evidence / 'Game.log'
    console_log = evidence / 'Game-console.log'
    command = [str(executable), '-Unattended', '-NoSound', '-NoSplash',
               '-AuroraViewAllowControl', '-AuroraViewHostPort=' + str(port),
               '-AuroraViewHostToken=' + token, '-abslog=' + str(game_log), '-stdout', '-FullStdOutLogOutput']
    command += ['-Windowed', '-ResX=640', '-ResY=480'] if rendered_browser else ['-NullRHI']
    result = {'started_utc': now(), 'executable': str(executable),
              'arguments': [arg.replace(token, '<redacted>') for arg in command[1:]],
              'actions': {}, 'pid': None, 'exit_code': None, 'forced_cleanup': False,
              'rendered_browser': 'failed' if rendered_browser else 'not_run'}
    process = None
    client = None
    try:
        with console_log.open('wb') as stream:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            process = subprocess.Popen(command, cwd=executable.parent, stdout=stream,
                                       stderr=subprocess.STDOUT, startupinfo=startup)
            result['pid'] = process.pid
            write_json(evidence / 'game-process.json', result)
            deadline = time.monotonic() + min(timeout, 120)
            while client is None:
                if process.poll() is not None:
                    raise build_plugin.BuildError('Packaged Game exited before its native control host became ready')
                try:
                    client = Client(port, token, expected_pid=process.pid,
                                    expected_engine=engine_version + '.', expected_context='game', timeout=5)
                except (OSError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise build_plugin.BuildError('Packaged Game control host did not become ready')
                    time.sleep(0.2)
            result['identity'] = client.identity
            info = client.call('unreal.engine.info')
            if (info.get('pid') != process.pid or info.get('context') != 'game'
                    or not info.get('engine_ready') or not info.get('native_control')
                    or not info.get('engine_version', '').startswith(engine_version + '.')):
                raise build_plugin.BuildError('Live Game engine identity or control capability differs from the fixture')
            result['actions']['engine_info'] = info
            worlds = client.call('unreal.world.list')
            if not isinstance(worlds, list) or not worlds:
                raise build_plugin.BuildError('Live packaged Game has no world')
            result['actions']['world_list'] = worlds
            reflected = client.call('unreal.object.call', {
                'object': '/Script/Engine.Default__KismetSystemLibrary', 'function': 'GetEngineVersion', 'args': {}})
            if not reflected.get('return_value', '').startswith(engine_version + '.'):
                raise build_plugin.BuildError('Native reflected UFunction did not return the current engine version')
            result['actions']['reflected_engine_version'] = reflected
            client.bind_call('python.acceptance.echo', lambda value: {'echo': value})
            reverse = client.call('python.acceptance.echo', {'value': 'packaged-game-round-trip'})
            if reverse != {'echo': 'packaged-game-round-trip'}:
                raise build_plugin.BuildError('External Python tool reverse call did not round-trip')
            result['actions']['python_reverse_call'] = reverse
            # A live connection must survive ticks with no incoming bytes.
            time.sleep(0.3)
            if client.call('python.acceptance.echo', {'value': 'idle-connection'}) != {'echo': 'idle-connection'}:
                raise build_plugin.BuildError('Native connection did not survive an idle interval')
            result['actions']['idle_connection'] = 'pass'
            try:
                with Client(port, secrets.token_urlsafe(32), expected_pid=process.pid, timeout=5):
                    raise build_plugin.BuildError('Native host accepted a different token')
            except ProtocolError:
                result['actions']['wrong_token_rejected'] = True
            with Client(port, token, expected_pid=process.pid, expected_context='game') as other:
                try:
                    other.bind_call('python.acceptance.echo', lambda value: value)
                    raise build_plugin.BuildError('Another connection replaced an owned Python tool')
                except RemoteError as error:
                    if error.code != 'TOOL_CONFLICT':
                        raise
                    result['actions']['tool_ownership'] = 'pass'
                other.bind_call('python.acceptance.ephemeral', lambda: True)
                if client.call('python.acceptance.ephemeral') is not True:
                    raise build_plugin.BuildError('Cross-connection native tool dispatch failed')
            disconnect_deadline = time.monotonic() + 5
            while True:
                try:
                    client.call('python.acceptance.ephemeral')
                except RemoteError as error:
                    if error.code == 'METHOD_NOT_FOUND':
                        break
                    if error.code not in ['TOOL_DISCONNECTED', 'TOOL_UNAVAILABLE']:
                        raise
                if time.monotonic() >= disconnect_deadline:
                    raise build_plugin.BuildError('Disconnected Python tool was not unregistered')
                time.sleep(0.05)
            result['actions']['disconnect_cleanup'] = 'pass'
            received = []
            event_ready = threading.Event()
            def on_event(data):
                received.append(data)
                event_ready.set()
            unsubscribe = client.on('acceptance:roundtrip', on_event)
            event_data = {'value': 'external-python-event', 'false_value': False, 'zero': 0}
            client.emit('acceptance:roundtrip', event_data)
            if not event_ready.wait(5) or received != [event_data]:
                raise build_plugin.BuildError('Bidirectional native/Python event did not round-trip')
            unsubscribe()
            result['actions']['bidirectional_event'] = received[0]
            if rendered_browser:
                result['actions']['rendered_browser'] = validate_browser(client, process, timeout)
                result['rendered_browser'] = 'pass'
            if client.call('auroraview.host.shutdown') is not True:
                raise build_plugin.BuildError('Native host did not acknowledge normal fixture shutdown')
            result['actions']['normal_shutdown'] = True
            try:
                result['exit_code'] = process.wait(timeout=min(timeout, 60))
            except subprocess.TimeoutExpired as error:
                raise build_plugin.BuildError('Game did not exit normally after host shutdown') from error
            if result['exit_code'] != 0:
                raise build_plugin.BuildError('Game exited with a nonzero code after validation')
    finally:
        if client is not None:
            client.close()
        if process is not None:
            result['forced_cleanup'] = process.poll() is None
            stop_owned(process)
            result['exit_code'] = process.returncode
        # Unreal logs its command line automatically. Never preserve the token.
        for path in [game_log, console_log]:
            if path.is_file():
                content = path.read_bytes()
                path.write_bytes(content.replace(token.encode('utf-8'), b'<redacted>'))
        result['completed_utc'] = now()
        write_json(evidence / 'game-process.json', result)
    return result


def validate(engine_root, package_root, output_root, timeout, rendered_browser=False):
    if os.name != 'nt':
        raise build_plugin.BuildError('Packaged Win64 Game validation requires Windows')
    engine, package, output = (Path(value).resolve() for value in [engine_root, package_root, output_root])
    for protected in [ROOT, engine, package]:
        if build_plugin.inside(output, protected) or build_plugin.inside(protected, output):
            raise build_plugin.BuildError('Output must be isolated from source, engine and package')
    if output.exists():
        raise build_plugin.BuildError('Output already exists; use a new validation directory')
    receipt_path, package_receipt, policy = verify_inputs(engine, package)
    output.mkdir(parents=True)
    evidence = output / 'evidence'
    evidence.mkdir()
    result_path = evidence / 'game-validation.json'
    result = {'schema_version': 1, 'status': 'failed', 'packaged_game': 'not_run', 'started_utc': now(),
              'rendered_browser': 'failed' if rendered_browser else 'not_run',
              'engine': package_receipt['engine'], 'source': package_receipt['source'],
              'build_receipt': str(receipt_path), 'build_receipt_sha256': build_plugin.sha256(receipt_path),
              'package_files_sha256': package_receipt['package']['files_sha256']}
    try:
        project = output / 'Project'
        project.mkdir()
        create_project(project, package, policy['version'])
        verify_package(project / 'Plugins/AuroraView', package_receipt['package']['files_sha256'])
        archive = output / 'PackagedGame'
        command = game_command(engine, project / f'{PROJECT}.uproject', archive, policy)
        overrides = build_plugin.build_environment(policy, output)
        environment = os.environ.copy()
        environment.update(overrides)
        before_config = build_plugin.configuration_inputs(engine)
        result.update(environment_overrides=overrides, ubt_configuration_sha256=before_config,
                      build_command=command, packaged_game='failed')
        write_json(result_path, result)
        log = output / 'uat.log'
        print('BuildCookRun log: ' + str(log), flush=True)
        run_logged(command, project, log, environment, timeout)
        result['uat_log_sha256'] = build_plugin.sha256(log)
        result['compiler_toolchains'] = build_plugin.compiler_evidence(log, policy)
        executable, stage = stage_evidence(project, archive, engine, package_receipt)
        result['stage'] = stage
        result['runtime'] = run_game(executable, evidence, policy['version'], timeout, rendered_browser)
        after_stage = inventory(archive)
        if any(after_stage.get(name) != digest for name, digest in stage['files_sha256'].items()):
            raise build_plugin.BuildError('A staged Game input changed during live validation')
        result['runtime_created_files_sha256'] = {
            name: digest for name, digest in after_stage.items() if name not in stage['files_sha256']}
        verify_inputs(engine, package)
        if build_plugin.sha256(receipt_path) != result['build_receipt_sha256']:
            raise build_plugin.BuildError('Package build receipt changed during validation')
        after_config = build_plugin.configuration_inputs(engine)
        result['ubt_configuration_after_sha256'] = after_config
        if any(after_config.get(path) != digest for path, digest in before_config.items()):
            raise build_plugin.BuildError('An existing UBT configuration changed during Game validation')
        scope = 'Cooked Development Win64 Game, native control and external Python tools'
        if rendered_browser:
            result['rendered_browser'] = 'pass'
            scope += '; rendered CEF Core whenReady/call/invoke, bidirectional browser events and view teardown passed'
        else:
            scope += '; rendered CEF/UI acceptance remains separate'
        result.update(status='passed', packaged_game='pass', scope=scope)
    except Exception as error:
        result['error'] = str(error)
    finally:
        result['completed_utc'] = now()
        write_json(result_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--rendered-browser', action='store_true',
                        help='Enable rendering and verify a real CEF Core/Python browser round-trip')
    args = parser.parse_args()
    if args.timeout < 30:
        parser.error('--timeout must be at least 30 seconds')
    try:
        result = validate(args.engine_root, args.package, args.output, args.timeout, args.rendered_browser)
    except (build_plugin.BuildError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'packaged_game': 'not_run', 'error': str(error)}))
        return 1
    print(json.dumps({'status': result['status'], 'packaged_game': result['packaged_game'],
                      'rendered_browser': result['rendered_browser'],
                      'error': result.get('error'), 'receipt': str(Path(args.output) / 'evidence/game-validation.json')}))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
