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
import pe_evidence
import preflight_engine


ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'AuroraViewGameFixture'
GAME_CONFIGURATIONS = ('Development', 'Shipping')


def validate_configuration(configuration):
    if configuration not in GAME_CONFIGURATIONS:
        raise build_plugin.BuildError('Game configuration must be Development or Shipping')
    return configuration


def executable_names(configuration):
    validate_configuration(configuration)
    # The generated fixture uses Unreal's default undecorated Development
    # configuration; Shipping must identify its own decorated native binary.
    names = [PROJECT + '-Win64-' + configuration + '.exe']
    if configuration == 'Development':
        names.append(PROJECT + '.exe')
    return names


def validate_game_target(target, policy, configuration):
    validate_configuration(configuration)
    if (target.get('TargetName') != PROJECT or target.get('Platform') != 'Win64'
            or target.get('Configuration') != configuration):
        raise build_plugin.BuildError('Game target receipt differs from the selected ' + configuration + ' Win64 fixture')
    target_type = target.get('TargetType')
    if target_type != 'Game' and not (target_type is None and policy['version'] == '4.18'):
        raise build_plugin.BuildError('Compiled target is not a Game target')


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


DDC_GRAPH = 'AuroraViewValidationDDC'
DDC_DIRECTORY = 'DerivedDataCache'


def configure_project_cache(project, version):
    """Select only a writable project-owned file cache, without shared overrides."""
    if version not in preflight_engine.SUPPORTED_VERSIONS:
        raise build_plugin.BuildError('Unsupported cache configuration version: ' + version)
    cache = project / DDC_DIRECTORY
    if version.startswith('4.') and len(str(cache.resolve())) > 119:
        raise build_plugin.BuildError('UE4 cache paths are limited to 119 characters; use a shorter output directory')
    local = ('Type=FileSystem,ReadOnly=false,Clean=false,Flush=false,DeleteUnused=false,'
             'Path="%GAMEDIR%' + DDC_DIRECTORY + '"')
    if version == '5.8':
        graph = ('\n[DerivedDataCacheGraphs]\n' + DDC_GRAPH + '=(AuroraViewValidationLocal)\n'
                 '[DerivedDataCacheStores]\nAuroraViewValidationLocal=(' + local + ')\n')
        syntax = 'cache_stores'
    else:
        graph = ('\n[' + DDC_GRAPH + ']\nRoot=(Type=KeyLength,Length=120,Inner=AsyncPut)\n'
                 'AsyncPut=(Type=AsyncPut,Inner=Local)\nLocal=(' + local + ',DeleteOnly=false)\n')
        syntax = 'backend_graph'
    config = project / 'Config/DefaultEngine.ini'
    config.write_text(config.read_text(encoding='utf-8') + graph, encoding='utf-8')
    # UE 5.8 enables the separate cooking ZenStore by default. Keep this
    # disposable/offline fixture independent of that shared service as well.
    if version.startswith('5.'):
        packaging = project / 'Config/DefaultGame.ini'
        packaging.write_text(packaging.read_text(encoding='utf-8') +
                             '\n[/Script/UnrealEd.ProjectPackagingSettings]\nbUseZenStore=False\n',
                             encoding='utf-8')
    cache.mkdir(parents=True)
    probe = cache / ('.write-probe-' + secrets.token_hex(8))
    created = False
    try:
        with probe.open('xb') as stream:
            created = True
            stream.write(b'AuroraView validation cache')
        if probe.read_bytes() != b'AuroraView validation cache':
            raise build_plugin.BuildError('Project cache readback failed')
    finally:
        if created:
            probe.unlink()
    return dict(graph=DDC_GRAPH, syntax=syntax, directory=str(cache),
                config_sha256=build_plugin.sha256(config), writable_probe='pass',
                shared_cache=False, zen=False, zen_store=False)


def create_project(root, package, version):
    # Match the installed engine defaults instead of overriding a shared Editor
    # build environment. These target settings do not exist in UE 4.18.
    major, minor = (int(part) for part in version.split('.')[:2])
    settings = ''
    if major >= 5:
        settings = ('        DefaultBuildSettings = BuildSettingsVersion.Latest;\n'
                    '        IncludeOrderVersion = EngineIncludeOrderVersion.Latest;\n')
    elif (major, minor) >= (4, 26):
        settings = '        DefaultBuildSettings = BuildSettingsVersion.V2;\n'
    source = root / 'Source' / PROJECT
    source.mkdir(parents=True)
    # UE4.18 staging enumerates Content even when the fixture cooks only Entry.
    (root / 'Content').mkdir()
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
        + settings +
        f'        ExtraModuleNames.Add("{PROJECT}");\n'
        '    }\n}\n', encoding='utf-8')
    (root / 'Source' / f'{PROJECT}Editor.Target.cs').write_text(
        'using UnrealBuildTool;\n'
        f'public class {PROJECT}EditorTarget : TargetRules {{\n'
        f'    public {PROJECT}EditorTarget(TargetInfo Target) : base(Target) {{\n'
        '        Type = TargetType.Editor;\n'
        + settings +
        f'        ExtraModuleNames.Add("{PROJECT}");\n'
        '    }\n}\n', encoding='utf-8')
    # The public demonstration and CI compile the same project-owned scene.
    # Engine target settings stay generated above because UE4 and UE5 differ.
    template = ROOT / 'examples' / PROJECT
    inventory(template)
    shutil.copytree(template / 'Source' / PROJECT, source, dirs_exist_ok=True)
    shutil.copytree(template / 'Config', root / 'Config')
    return configure_project_cache(root, version)


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
        try:
            write_json(path, process_receipt)
            process_receipt['exit_code'] = process.wait(timeout=timeout)
            if process.returncode:
                raise build_plugin.BuildError(f'Native build exited {process.returncode}; see {log}')
        except subprocess.TimeoutExpired as error:
            process_receipt['timed_out'] = True
            raise build_plugin.BuildError(f'Native build exceeded {timeout} seconds') from error
        finally:
            primary_error = sys.exc_info()[1]
            try:
                try:
                    stop_owned(process)
                finally:
                    process_receipt.update(exit_code=process.returncode, completed_utc=now())
                    write_json(path, process_receipt)
            except Exception as cleanup_error:
                if primary_error is not None:
                    raise primary_error from cleanup_error
                raise


def editor_command(engine, project, policy, log_file):
    command = [str(engine / 'Engine/Build/BatchFiles/Build.bat'), PROJECT + 'Editor',
               'Win64', 'Development', '-Project=' + str(project), '-NoHotReload', '-log=' + str(log_file)]
    if policy['version'] != '4.18':
        # Outputs belong to this disposable project. No engine or loaded project
        # DLL is replaced, even when another Editor has its Live Coding mutex.
        command.extend(['-NoHotReloadFromIDE', '-NoEngineChanges'])
    if policy['version'] == '4.26':
        command.append('-2019')
    return command


def game_command(engine, project, archive, policy, configuration='Development'):
    validate_configuration(configuration)
    command = [str(engine / 'Engine/Build/BatchFiles/RunUAT.bat'), 'BuildCookRun',
               '-project=' + str(project), '-target=' + PROJECT, '-noP4', '-platform=Win64',
               '-clientconfig=' + configuration, '-build', '-nocompileeditor', '-cook', '-stage', '-pak', '-package',
               '-archive', '-prereqs', '-archivedirectory=' + str(archive), '-map=/Engine/Maps/Entry',
               '-unattended', '-utf8output', '-AdditionalCookerOptions=-ddc=' + DDC_GRAPH]
    if policy['version'] == '4.26':
        command.append('-ubtargs=-2019 -NoHotReloadFromIDE')
    elif not policy['version'].startswith('4.'):
        command.append('-ubtargs=-NoHotReloadFromIDE')
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


def staged_executable_evidence(project, executable, target, policy, configuration='Development'):
    validate_game_target(target, policy, configuration)
    if executable.name not in executable_names(configuration):
        raise build_plugin.BuildError('Staged Game executable differs from the selected configuration')
    products = target.get('BuildProducts', [])
    if not isinstance(products, list):
        raise build_plugin.BuildError('Invalid Game target BuildProducts')
    products = [product for product in products if isinstance(product, dict) and product.get('Type') == 'Executable']
    if len(products) != 1 or not isinstance(products[0].get('Path'), str):
        raise build_plugin.BuildError('Expected one receipt-bound compiled Game executable')
    raw = products[0]['Path'].replace('\\', '/')
    if raw.startswith('$(ProjectDir)/'):
        compiled = project / raw[len('$(ProjectDir)/'):]
    elif '$(' not in raw and Path(raw).is_absolute():
        compiled = Path(raw)
    else:
        raise build_plugin.BuildError('Game executable receipt must identify a project-local file')
    compiled = compiled.resolve()
    if (compiled.parent != (project / 'Binaries/Win64').resolve()
            or compiled.name not in executable_names(configuration)
            or not compiled.is_file()):
        raise build_plugin.BuildError('Game executable receipt escapes the project Game output')
    digest, compiled_digest = build_plugin.sha256(executable), build_plugin.sha256(compiled)
    if digest == compiled_digest:
        return dict(comparison='whole_file', compiled_path=str(compiled), staged_path=str(executable),
                    compiled_sha256=compiled_digest, staged_sha256=digest)
    if policy['version'] != '4.18':
        raise build_plugin.BuildError('Staged executable differs from the actual compiled Game')
    try:
        return pe_evidence.compare_resource_update(compiled, executable)
    except (OSError, ValueError, struct.error) as error:
        raise build_plugin.BuildError('Staged executable differs from the actual compiled Game: ' + str(error)) from error


def stage_evidence(project, archive, engine, package_receipt, policy, configuration='Development'):
    validate_configuration(configuration)
    files = inventory(archive)
    if not files:
        raise build_plugin.BuildError('BuildCookRun produced no archived Game files')
    for name in files:
        if Path(name).suffix.lower() in ['.dll', '.exe', '.lib', '.obj'] and any(
                forbidden in Path(name).name.lower() for forbidden in ['auroravieweditor', 'unrealeditor', 'ue4editor']):
            raise build_plugin.BuildError('Editor binary was staged into the Game: ' + name)
    candidates = [archive / name for name in files if '/Binaries/Win64/' in '/' + name
                  and (Path(name).name == PROJECT + '.exe'
                       or (Path(name).name.startswith(PROJECT + '-Win64-') and Path(name).suffix == '.exe'))]
    if len(candidates) != 1 or candidates[0].name not in executable_names(configuration):
        raise build_plugin.BuildError('Expected one actual ' + configuration + ' Game executable under Binaries/Win64')
    executable = candidates[0]
    validate_executable(executable)
    target_paths = list((project / 'Binaries/Win64').glob('*.target'))
    targets = []
    for path in target_paths:
        target = build_plugin.read_json(path)
        if target.get('TargetName') == PROJECT:
            targets.append((path, target))
    if len(targets) != 1:
        raise build_plugin.BuildError('Missing unique actual ' + configuration + ' Win64 Game target receipt')
    target_path, target = targets[0]
    validate_game_target(target, policy, configuration)
    digest = build_plugin.sha256(executable)
    executable_identity = staged_executable_evidence(project, executable, target, policy, configuration)
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
    cef_relative = Path('Engine/Binaries/ThirdParty/CEF3/Win64')
    cef_prefix = (executable.parents[3].relative_to(archive) / cef_relative).as_posix() + '/'
    # UE5.5 stages icudtl.dat in both Win64 and Win64/Resources. Bind every
    # resource to its installed relative path rather than assuming unique names.
    for name, sha in cef.items():
        installed = engine / cef_relative / name[len(cef_prefix):]
        if (not name.startswith(cef_prefix) or not installed.is_file()
                or build_plugin.sha256(installed) != sha):
            raise build_plugin.BuildError('Staged CEF runtime differs from the installed engine path: ' + name)
    required_files = ['libcef.dll', 'icudtl.dat']
    required_files += (['cef.pak', 'cef_100_percent.pak', 'cef_200_percent.pak',
                        'cef_extensions.pak', 'devtools_resources.pak', 'natives_blob.bin', 'snapshot_blob.bin']
                       if policy['version'].startswith('4.') else ['resources.pak'])
    for required in required_files:
        if not any(Path(name).name == required for name in cef):
            raise build_plugin.BuildError('Staged CEF runtime is missing or differs from the installed engine: ' + required)
    helper = 'UnrealCEFSubProcess.exe' if policy['version'].startswith('4.') else 'EpicWebHelper.exe'
    subprocess_matches = [(name, sha) for name, sha in files.items()
                          if name.endswith('/Engine/Binaries/Win64/' + helper)]
    installed_subprocess = engine / 'Engine/Binaries/Win64' / helper
    if (len(subprocess_matches) != 1 or not installed_subprocess.is_file()
            or subprocess_matches[0][1] != build_plugin.sha256(installed_subprocess)):
        raise build_plugin.BuildError('Staged CEF subprocess is missing or differs from the installed engine')
    cef.update(subprocess_matches)
    return executable, {'configuration': configuration, 'files_sha256': files, 'runtime_resources_sha256': resources,
                        'cef_sha256': cef, 'executable_sha256': digest,
                        'executable_identity': executable_identity,
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


def game_execution_policy(engine_version, rendered_browser):
    if rendered_browser:
        return 'rendered_browser', ['-Windowed', '-ResX=640', '-ResY=480']
    if engine_version == '5.5':
        # Stock UE5.5 NullRHI can release Nanite's GPUMessage socket after its
        # owner during CRT teardown. An initialized offscreen RHI exits cleanly.
        return 'offscreen_d3d11', [
            '-RenderOffscreen', '-d3d11', '-AllowSoftwareRendering',
            '-Windowed', '-ResX=64', '-ResY=64']
    return 'null_rhi', ['-NullRHI']


def run_game(executable, evidence, engine_version, timeout, rendered_browser=False, *,
             configuration='Development', expected_sha256=None):
    validate_configuration(configuration)
    if executable.name not in executable_names(configuration):
        raise build_plugin.BuildError('Game process executable differs from the selected configuration')
    executable_sha256 = build_plugin.sha256(executable)
    if expected_sha256 is not None and executable_sha256 != expected_sha256:
        raise build_plugin.BuildError('Game executable changed after staged validation')
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
    execution_mode, graphics_arguments = game_execution_policy(engine_version, rendered_browser)
    command += graphics_arguments
    result = {'started_utc': now(), 'executable': str(executable), 'configuration': configuration,
              'executable_sha256': executable_sha256,
              'arguments': [arg.replace(token, '<redacted>') for arg in command[1:]],
              'actions': {}, 'pid': None, 'exit_code': None, 'forced_cleanup': False,
              'rendered_browser': 'failed' if rendered_browser else 'not_run',
              'execution_mode': execution_mode}
    process = None
    client = None
    failed = False
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
                    or info.get('editor_python') is not False
                    or not info.get('engine_version', '').startswith(engine_version + '.')):
                raise build_plugin.BuildError('Live Game engine identity or control capability differs from the fixture')
            result['actions']['engine_info'] = info
            worlds = client.call('unreal.world.list')
            if not isinstance(worlds, list) or not worlds:
                raise build_plugin.BuildError('Live packaged Game has no world')
            result['actions']['world_list'] = worlds
            import demo_tools
            scene = demo_tools.find_scene(client, 'game')
            if scene is None:
                raise build_plugin.BuildError('The packaged public demo has no native scene')
            before = demo_tools.scene_state(client, scene[1])
            scene_tools = demo_tools.DemoTools(client, scene[1])
            raised = scene_tools.set_height(150)
            restored_scene = scene_tools.reset()
            if (before['height'] != 0 or raised['height'] != 150
                    or restored_scene['height'] != 0 or raised['revision'] <= before['revision']
                    or restored_scene['revision'] <= raised['revision']):
                raise build_plugin.BuildError('Native demo scene mutation/readback/reset failed')
            result['actions']['demo_scene'] = {'world': scene[0], 'object': scene[1],
                                              'before': before, 'raised': raised, 'reset': restored_scene}
            reflected = client.call('unreal.object.call', {
                'object': '/Script/Engine.Default__KismetSystemLibrary', 'function': 'GetEngineVersion', 'args': {}})
            if not reflected.get('return_value', '').startswith(engine_version + '.'):
                raise build_plugin.BuildError('Native reflected UFunction did not return the current engine version')
            result['actions']['reflected_engine_version'] = reflected
            property_args = {'object': '/Script/Engine.Default__GameUserSettings', 'property': 'bUseVSync'}
            original = client.call('unreal.object.get', property_args)
            if not isinstance(original, bool):
                raise build_plugin.BuildError('Native Game property did not return its reflected boolean type')
            try:
                changed = client.call('unreal.object.set', dict(property_args, value=not original))
                readback = client.call('unreal.object.get', property_args)
                if changed is not (not original) or readback is not changed:
                    raise build_plugin.BuildError('Native Game property mutation did not survive independent readback')
            finally:
                restored = client.call('unreal.object.set', dict(property_args, value=original))
                if restored is not original:
                    raise build_plugin.BuildError('Native Game property restoration failed')
            result['actions']['reflected_property_roundtrip'] = {
                **property_args, 'original': original, 'changed': changed, 'readback': readback, 'restored': restored}
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
    except Exception as error:
        failed = True
        result['error'] = str(error).replace(token, '<redacted>')
        # A subprocess exception can include the launch command and its token.
        raise build_plugin.BuildError(result['error']) from None
    finally:
        cleanup_errors = []
        if client is not None:
            try:
                client.close()
            except Exception as error:
                cleanup_errors.append('Client cleanup: ' + str(error).replace(token, '<redacted>'))
        if process is not None:
            result['forced_cleanup'] = process.poll() is None
            try:
                stop_owned(process)
            except Exception as error:
                cleanup_errors.append('Process cleanup: ' + str(error).replace(token, '<redacted>'))
            result['exit_code'] = process.returncode
        # Unreal logs its command line automatically. Never preserve the token.
        for path in [game_log, console_log]:
            try:
                if path.is_file():
                    content = path.read_bytes()
                    path.write_bytes(content.replace(token.encode('utf-8'), b'<redacted>'))
            except OSError as error:
                cleanup_errors.append('Log redaction: ' + str(error).replace(token, '<redacted>'))
        if cleanup_errors:
            result['cleanup_errors'] = cleanup_errors
            if not failed:
                result['error'] = 'Packaged Game cleanup failed: ' + '; '.join(cleanup_errors)
        result['completed_utc'] = now()
        write_json(evidence / 'game-process.json', result)
        if cleanup_errors and not failed:
            raise build_plugin.BuildError(result['error']) from None
    return result


def validate(engine_root, package_root, output_root, timeout, rendered_browser=False, configuration='Development'):
    validate_configuration(configuration)
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
              'configuration': configuration,
              'rendered_browser': 'failed' if rendered_browser else 'not_run',
              'engine': package_receipt['engine'], 'source': package_receipt['source'],
              'build_receipt': str(receipt_path), 'build_receipt_sha256': build_plugin.sha256(receipt_path),
              'package_files_sha256': package_receipt['package']['files_sha256']}
    try:
        project = output / 'Project'
        project.mkdir()
        cache = create_project(project, package, policy['version'])
        result['derived_data_cache'] = cache
        verify_package(project / 'Plugins/AuroraView', package_receipt['package']['files_sha256'])
        archive = output / 'PackagedGame'
        command = game_command(engine, project / f'{PROJECT}.uproject', archive, policy, configuration)
        overrides = build_plugin.build_environment(policy, output)
        environment = os.environ.copy()
        environment.update(overrides)
        before_config = build_plugin.configuration_inputs(engine)
        prepare_command = editor_command(engine, project / f'{PROJECT}.uproject', policy, output / 'ubt-editor.log')
        result.update(environment_overrides=overrides, ubt_configuration_sha256=before_config,
                      editor_build_command=prepare_command, build_command=command, packaged_game='failed')
        write_json(result_path, result)
        editor_log = output / 'editor-build.log'
        print('Fixture Editor build log: ' + str(editor_log), flush=True)
        run_logged(prepare_command, project, editor_log, environment, timeout)
        result['editor_build_log_sha256'] = build_plugin.sha256(editor_log)
        result['editor_compiler_toolchains'] = build_plugin.compiler_evidence(editor_log, policy)
        log = output / 'uat.log'
        print('BuildCookRun log: ' + str(log), flush=True)
        run_logged(command, project, log, environment, timeout)
        result['uat_log_sha256'] = build_plugin.sha256(log)
        result['compiler_toolchains'] = build_plugin.compiler_evidence(log, policy)
        executable, stage = stage_evidence(project, archive, engine, package_receipt, policy, configuration)
        result['stage'] = stage
        if (stage.get('configuration') != configuration
                or stage.get('target', {}).get('Configuration') != configuration
                or executable.name not in executable_names(configuration)):
            raise build_plugin.BuildError('Staged evidence differs from the requested Game configuration')
        result['runtime'] = run_game(executable, evidence, policy['version'], timeout, rendered_browser,
                                     configuration=configuration, expected_sha256=stage['executable_sha256'])
        if (result['runtime'].get('configuration') != configuration
                or result['runtime'].get('executable') != str(executable)
                or result['runtime'].get('executable_sha256') != stage['executable_sha256']):
            raise build_plugin.BuildError('Game process evidence differs from the staged configuration or executable')
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
        scope = 'Cooked ' + configuration + ' Win64 Game, native control and external Python tools'
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
    parser.add_argument('--configuration', choices=GAME_CONFIGURATIONS, default='Development',
                        help='Cook and run this Game configuration; the Editor cooker stays Development')
    parser.add_argument('--rendered-browser', action='store_true',
                        help='Enable rendering and verify a real CEF Core/Python browser round-trip')
    args = parser.parse_args()
    if args.timeout < 30:
        parser.error('--timeout must be at least 30 seconds')
    try:
        result = validate(args.engine_root, args.package, args.output, args.timeout, args.rendered_browser, args.configuration)
    except (build_plugin.BuildError, OSError, ValueError) as error:
        print(json.dumps({'status': 'failed', 'packaged_game': 'not_run',
                          'configuration': args.configuration, 'error': str(error)}))
        return 1
    print(json.dumps({'status': result['status'], 'packaged_game': result['packaged_game'],
                      'configuration': result['configuration'],
                      'rendered_browser': result['rendered_browser'],
                      'error': result.get('error'), 'receipt': str(Path(args.output) / 'evidence/game-validation.json')}))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
