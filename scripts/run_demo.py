#!/usr/bin/env python3
"""Build and keep a real AuroraView Editor/Game demonstration running.

No embedded Unreal Python, npm, network service or UI simulation is required.
The packaged bundle path additionally needs no Unreal installation/compiler.
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
import subprocess
import sys
import threading
import time

import build_plugin
import demo_tools
import preflight_engine
import validate_game
from owner_dispatch import OwnerDispatcher

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from auroraview_unreal import Client, ConnectionClosedError, RemoteError


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)


def validate_startup_timeout(seconds):
    if type(seconds) is not int or not 30 <= seconds <= 3600:
        raise build_plugin.BuildError('Use startup-timeout between 30 and 3600 seconds')


class StartupDeadline:
    """One readiness budget; build time, ready lifetime and teardown are separate."""

    def __init__(self, seconds, receipt):
        validate_startup_timeout(seconds)
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.receipt = receipt
        receipt.update(timeout_seconds=seconds, phase='endpoint', elapsed_seconds=0)

    def remaining(self, phase=None, maximum=None):
        if phase is not None:
            self.receipt['phase'] = phase
        current = time.monotonic()
        self.receipt['elapsed_seconds'] = max(0, current - self.started)
        remaining = self.deadline - current
        if remaining <= 0:
            raise build_plugin.BuildError('Demo startup timeout during ' + self.receipt['phase'])
        return min(remaining, maximum) if maximum is not None else remaining

    def call(self, client, method, *args, **kwargs):
        kwargs['timeout'] = self.remaining(maximum=min(kwargs.get('timeout', 5), 5))
        result = client.call(method, *args, **kwargs)
        self.remaining()
        return result


class StartupCalls:
    """Call-only adapter for native readiness queries; owns no transport."""

    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline

    def call(self, method, *args, **kwargs):
        return self.deadline.call(self.client, method, *args, **kwargs)


def editor_runtime_cache(prepared, output):
    """Reuse only this output's ordinary private cache, never a caller-supplied path."""
    output = Path(output)
    cache = output / 'RuntimeDerivedDataCache'
    for path in (cache, *cache.parents):
        if path.exists() or path.is_symlink():
            attributes = path.lstat()
            if (path.is_symlink() or getattr(attributes, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or not path.is_dir()):
                raise build_plugin.BuildError('Runtime cache requires ordinary directory ancestors')
    protected = [ROOT]
    protected += [Path(prepared[name]) for name in ('engine_root', 'package') if prepared.get(name)]
    for path in protected:
        if build_plugin.inside(cache.resolve(), path.resolve()) or build_plugin.inside(path.resolve(), cache.resolve()):
            raise build_plugin.BuildError('Runtime cache overlaps a protected input')
    if any(character in str(cache) for character in ['"', '%', '\r', '\n', '\0']):
        raise build_plugin.BuildError('Runtime cache path is not safe for the private INI graph')
    if prepared['engine_version'].startswith('4.') and len(str(cache.resolve())) > 119:
        raise build_plugin.BuildError('UE4 cache paths are limited to 119 characters; use a shorter output directory')
    if prepared.get('derived_data_cache', {}).get('graph') != validate_game.DDC_GRAPH:
        raise build_plugin.BuildError('Prepared Editor has no verified private cache graph')
    cache.mkdir(exist_ok=True)
    probe = cache / ('.write-probe-' + secrets.token_hex(8))
    created = False
    try:
        with probe.open('xb') as stream:
            created = True
            stream.write(b'AuroraView runtime cache')
        if probe.read_bytes() != b'AuroraView runtime cache':
            raise build_plugin.BuildError('Runtime cache readback failed')
    finally:
        if created:
            probe.unlink()
    return cache.resolve()


def isolated_output(output, protected, reuse=False):
    output = Path(output).resolve()
    for path in protected:
        path = Path(path).resolve()
        if build_plugin.inside(output, path) or build_plugin.inside(path, output):
            raise build_plugin.BuildError('Choose an output separate from source, engine, bundle and package')
    if reuse:
        if not output.is_dir():
            raise build_plugin.BuildError('--reuse requires an existing prepared demo output')
    else:
        output.mkdir(parents=True, exist_ok=False)
    return output


def relocated_package(engine, package):
    """Accept an intact downloaded package without rewriting its provenance."""
    receipt_path = package.parent / 'build-receipt.json'
    receipt = build_plugin.read_json(receipt_path)
    version_path = engine / 'Engine/Build/Build.version'
    policy = preflight_engine.engine_policy(build_plugin.read_json(version_path))
    modules_path = engine / f'Engine/Binaries/Win64/{policy["editor_target"]}.modules'
    identity = receipt.get('engine', {})
    if (receipt.get('status') != 'pass' or receipt.get('unreal_compile') != 'pass'
            or identity.get('build_id') != build_plugin.read_json(modules_path).get('BuildId')
            or identity.get('version_sha256') != build_plugin.sha256(version_path)
            or identity.get('modules_sha256') != build_plugin.sha256(modules_path)):
        raise build_plugin.BuildError('Plugin receipt does not match this installed engine')
    validate_game.verify_package(package, receipt['package']['files_sha256'])
    build_plugin.package_evidence(package, receipt['source_assets_sha256'], identity['build_id'], policy)
    # The demo must not silently use a different Runtime/Editor implementation.
    relevant = ('Source/', 'Resources/', 'ThirdParty/AuroraViewCore/')
    source_files = receipt.get('source', {}).get('files', {})
    current_files = {path.relative_to(ROOT).as_posix(): build_plugin.sha256(path)
                     for directory in relevant for path in (ROOT / directory).rglob('*') if path.is_file()}
    current_files['AuroraView.uplugin'] = build_plugin.sha256(ROOT / 'AuroraView.uplugin')
    recorded_files = {name: digest for name, digest in source_files.items()
                      if name.startswith(relevant) or name == 'AuroraView.uplugin'}
    if current_files != recorded_files:
        raise build_plugin.BuildError('Plugin sources/assets differ from the package provenance')
    for relative in source_files:
        if relative.startswith(relevant) or relative == 'AuroraView.uplugin':
            current = ROOT / relative
            if not current.is_file() or build_plugin.sha256(current) != source_files[relative]:
                raise build_plugin.BuildError('Package and local plugin source differ: ' + relative)
    if not any(name.startswith('Source/') for name in source_files):
        raise build_plugin.BuildError('Plugin receipt has no source provenance')
    return receipt, policy, receipt_path


def prepare(engine, package, output, mode, timeout, reuse):
    identity = build_plugin.git_identity(ROOT)
    if identity['dirty']:
        raise build_plugin.BuildError('Commit the source before building a reproducible demo')
    manifest_path = output / 'prepared-demo.json'
    if reuse:
        prepared = build_plugin.read_json(manifest_path)
        if (prepared.get('status') != 'prepared' or prepared.get('mode') != mode
                or prepared.get('source') != identity or Path(prepared.get('engine_root', '')).resolve() != engine):
            raise build_plugin.BuildError('Prepared demo differs from this source, engine or mode')
        package = Path(prepared['package'])
    elif package is None:
        build = build_plugin.build(engine, output / 'PluginBuild', require_clean=True)
        if build.get('status') != 'pass':
            raise build_plugin.BuildError('Plugin build failed; see PluginBuild/build-receipt.json')
        package = output / 'PluginBuild/Package'
    package = package.resolve()
    receipt, policy, receipt_path = relocated_package(engine, package)
    version = policy['version']
    if reuse:
        for relative, digest in prepared['products_sha256'].items():
            path = output / relative
            if not path.is_file() or build_plugin.sha256(path) != digest:
                raise build_plugin.BuildError('Prepared demo product changed: ' + relative)
        if prepared['build_receipt_sha256'] != build_plugin.sha256(receipt_path):
            raise build_plugin.BuildError('Prepared plugin receipt changed')
        if prepared['executable_sha256'] != build_plugin.sha256(Path(prepared['executable'])):
            raise build_plugin.BuildError('Prepared host executable changed')
        return prepared
    project = output / 'Project'
    project.mkdir()
    cache = validate_game.create_project(project, package, version)
    uproject = project / f'{validate_game.PROJECT}.uproject'
    environment = os.environ.copy()
    environment.update(build_plugin.build_environment(policy, output))
    before_config = build_plugin.configuration_inputs(engine)
    command = validate_game.editor_command(engine, uproject, policy, output / 'ubt-editor.log')
    print('Building demo Editor: ' + str(output / 'editor-build.log'), flush=True)
    validate_game.run_logged(command, project, output / 'editor-build.log', environment, timeout)
    stage = None
    if mode == 'game':
        command = validate_game.game_command(engine, uproject, output / 'PackagedGame', policy)
        print('Cooking and packaging demo: ' + str(output / 'uat.log'), flush=True)
        validate_game.run_logged(command, project, output / 'uat.log', environment, timeout)
        executable, stage = validate_game.stage_evidence(project, output / 'PackagedGame', engine, receipt, policy)
    else:
        executable = engine / f'Engine/Binaries/Win64/{policy["editor_target"]}.exe'
    after_config = build_plugin.configuration_inputs(engine)
    if any(after_config.get(name) != digest for name, digest in before_config.items()):
        raise build_plugin.BuildError('An existing UBT configuration changed during the build')
    if build_plugin.git_identity(ROOT) != identity:
        raise build_plugin.BuildError('Source changed during demo preparation')
    products = {}
    roots = [output / 'PackagedGame'] if mode == 'game' else [project / 'Binaries', project / 'Plugins/AuroraView']
    roots += [project / 'Source', project / 'Config']
    for directory in roots:
        for relative, digest in validate_game.inventory(directory).items():
            products[(directory / relative).relative_to(output).as_posix()] = digest
    products[uproject.relative_to(output).as_posix()] = build_plugin.sha256(uproject)
    prepared = dict(status='prepared', mode=mode, source=identity, engine=receipt['engine'],
                    engine_root=str(engine), engine_version=version, package=str(package),
                    build_receipt=str(receipt_path), build_receipt_sha256=build_plugin.sha256(receipt_path),
                    uproject=str(uproject), executable=str(executable), executable_sha256=build_plugin.sha256(executable), stage=stage,
                    products_sha256=products, derived_data_cache=cache, ubt_configuration_sha256=before_config,
                    ubt_configuration_after_sha256=after_config, completed_utc=now())
    save(manifest_path, prepared)
    return prepared


def bundle_inputs(bundle):
    manifest = build_plugin.read_json(bundle / 'demo-package.json')
    if manifest.get('schema_version') != 1 or manifest.get('configuration') != 'Development':
        raise build_plugin.BuildError('Unsupported demo bundle manifest')
    expected = manifest.get('files_sha256')
    if not isinstance(expected, dict) or not expected:
        raise build_plugin.BuildError('Demo bundle has no file inventory')
    actual = validate_game.inventory(bundle)
    actual = {name: digest for name, digest in actual.items()
              if name != 'demo-package.json' and '__pycache__' not in PurePosixPath(name).parts}
    if actual != expected:
        raise build_plugin.BuildError('Demo bundle inventory differs; extract a fresh complete bundle')
    for name, digest in expected.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or any(part.rstrip('. ') != part or not part for part in name.split('/'))
                or not isinstance(digest, str) or len(digest) != 64):
            raise build_plugin.BuildError('Unsafe demo bundle inventory entry')
        path = bundle / name
        if (not build_plugin.inside(path.resolve(), bundle) or not path.is_file()
                or path.is_symlink() or build_plugin.sha256(path) != digest):
            raise build_plugin.BuildError('Demo bundle file missing or changed: ' + name)
    executable = bundle / manifest.get('executable', '')
    html = bundle / 'Resources/live_demo.html'
    if (manifest.get('executable') not in expected or 'Resources/live_demo.html' not in expected
            or not manifest.get('engine_version') in preflight_engine.SUPPORTED_VERSIONS):
        raise build_plugin.BuildError('Demo bundle has no bound executable, HTML or supported version')
    validate_game.validate_executable(executable)
    return dict(mode='game', executable=str(executable), engine_version=manifest['engine_version'],
                source=manifest.get('source'), bundle_manifest_sha256=build_plugin.sha256(bundle / 'demo-package.json')), html


def editor_session_project(prepared, session_dir, runtime_cache=None):
    """Keep verified build inputs immutable when Editor writes project config."""
    source_root = Path(prepared['uproject']).resolve().parent.parent
    destination = session_dir / 'Project'
    destination.mkdir()
    copied = {}
    for name, digest in prepared['products_sha256'].items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or any(not part or part.rstrip('. ') != part for part in name.split('/'))):
            raise build_plugin.BuildError('Unsafe prepared product path: ' + name)
        if relative.parts[0] != 'Project' or 'Intermediate' in relative.parts:
            continue
        source = source_root / relative
        if (not build_plugin.inside(source.resolve(), source_root / 'Project')
                or source.is_symlink() or not source.is_file() or build_plugin.sha256(source) != digest):
            raise build_plugin.BuildError('Prepared Editor input changed: ' + name)
        target = session_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if build_plugin.sha256(target) != digest:
            raise build_plugin.BuildError('Editor session copy changed: ' + name)
        copied[name] = digest
    project = destination / Path(prepared['uproject']).name
    if project.relative_to(session_dir).as_posix() not in copied:
        raise build_plugin.BuildError('Prepared Editor has no recorded project descriptor')
    (destination / 'Content').mkdir(exist_ok=True)
    inputs = dict(products_sha256=copied)
    if runtime_cache is not None:
        runtime_cache = Path(runtime_cache)
        if runtime_cache != (session_dir.parent / 'RuntimeDerivedDataCache').resolve():
            raise build_plugin.BuildError('Runtime cache must belong to this prepared output')
        config = destination / 'Config/DefaultEngine.ini'
        before = config.read_bytes()
        portable = ('Path="%GAMEDIR%' + validate_game.DDC_DIRECTORY + '"').encode('utf-8')
        if before.count(portable) != 1 or 'Project/Config/DefaultEngine.ini' not in copied:
            raise build_plugin.BuildError('Prepared Editor must contain one verified portable private cache path')
        # Change only the verified session copy. The build's Config and products
        # remain byte-identical and the override has its own provenance below.
        config.write_bytes(before.replace(portable, ('Path="' + runtime_cache.as_posix() + '"').encode('utf-8')))
        inputs['runtime_overrides'] = {'Project/Config/DefaultEngine.ini': dict(
            input_sha256=copied['Project/Config/DefaultEngine.ini'], sha256=build_plugin.sha256(config),
            graph=validate_game.DDC_GRAPH, cache_directory=str(runtime_cache))}
    save(session_dir / 'editor-inputs.json', inputs)
    return project


def dock_editor_view(client, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.call('editor.view.dock', {'id': 'LiveDemo'})
        except RemoteError as error:
            if error.code != 'EDITOR_UNAVAILABLE' or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def wait_for_browser(tools, process, timeout=45, dispatcher=None):
    """Pump the caller's existing owner thread while workers await tool calls."""
    deadline = time.monotonic() + timeout
    while not tools.browser_ready.is_set():
        if process.poll() is not None or time.monotonic() >= deadline:
            return False
        if dispatcher:
            dispatcher.pump()
        tools.browser_ready.wait(0.02)
    return True


def launch(prepared, html, output, session_seconds, shared_tools=False, startup_timeout=900):
    validate_startup_timeout(startup_timeout)
    if shared_tools:
        demo_tools.require_shared_tools()
    session_dir = output / ('Session-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3))
    session_dir.mkdir()
    receipt_path = session_dir / 'session.json'
    token = secrets.token_urlsafe(32)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    mode, executable = prepared['mode'], Path(prepared['executable'])
    runtime_cache = editor_runtime_cache(prepared, output) if mode == 'editor' else None
    uproject = editor_session_project(prepared, session_dir, runtime_cache) if mode == 'editor' else None
    command = [str(executable)]
    if mode == 'editor':
        command += [str(uproject), '/Engine/Maps/Entry']
        if prepared.get('derived_data_cache', {}).get('graph') == validate_game.DDC_GRAPH:
            command += ['-ddc=' + validate_game.DDC_GRAPH]
    command += ['-AuroraViewDemo', '-AuroraViewAllowControl', '-AuroraViewHostPort=' + str(port),
                '-AuroraViewHostToken=' + token, '-NoSplash', '-NoSound', '-NoLiveCoding',
                '-Windowed', '-ResX=1000', '-ResY=720', '-d3d11', '-NoVSync',
                '-abslog=' + str(session_dir / 'Unreal.log'), '-stdout', '-FullStdOutLogOutput']
    result = dict(status='starting', mode=mode, started_utc=now(), source=prepared.get('source'),
                  engine_version=prepared['engine_version'], pid=None, port=port,
                  arguments=[arg.replace(token, '<redacted>') for arg in command[1:]],
                  browser_ready=False, events=[], exit_code=None, forced_cleanup=False,
                  startup=dict(timeout_seconds=startup_timeout, phase='not_started', elapsed_seconds=0))
    if uproject:
        result['uproject'] = str(uproject)
        if prepared.get('derived_data_cache', {}).get('graph') == validate_game.DDC_GRAPH:
            result['derived_data_cache'] = dict(
                graph=validate_game.DDC_GRAPH,
                expected_directory=str(runtime_cache), writable_probe='pass', native_usage='not_verified',
                shared_cache=False, zen=False, zen_store=False,
                runtime_overrides=build_plugin.read_json(session_dir / 'editor-inputs.json')['runtime_overrides'])
    lock = threading.Lock()

    def record(kind, data):
        with lock:
            result['events'].append(dict(kind=kind, data=data, utc=now()))
            result['events'] = result['events'][-128:]
            if kind == 'browser.ready':
                result['browser_ready'] = True
            save(receipt_path, result)

    process = client = tools = None
    dispatcher = OwnerDispatcher() if shared_tools else None
    try:
        with (session_dir / 'console.log').open('wb') as stream:
            process = subprocess.Popen(command, cwd=executable.parent, stdout=stream, stderr=subprocess.STDOUT)
        result['pid'] = process.pid
        save(receipt_path, result)
        startup = StartupDeadline(startup_timeout, result['startup'])
        save(receipt_path, result)
        while client is None:
            remaining = startup.remaining('endpoint', maximum=5)
            if process.poll() is not None:
                raise build_plugin.BuildError('Demo host exited before native communication became ready')
            try:
                client = Client(port, token, expected_pid=process.pid, expected_context=mode,
                                expected_engine=prepared['engine_version'] + '.', timeout=remaining)
            except (OSError, TimeoutError):
                time.sleep(min(0.2, startup.remaining()))
        # A last-moment hello must not shorten this client's ready/cleanup RPCs.
        # Startup queries continue to pass their own remaining timeout below.
        client.timeout = 5.0
        startup.remaining('scene')
        save(receipt_path, result)
        bounded_client = StartupCalls(client, startup)
        result['identity'] = client.identity
        scene = None
        command_sent = False
        while scene is None:
            info = bounded_client.call('unreal.engine.info')
            if info.get('engine_ready'):
                if mode == 'editor' and not command_sent:
                    worlds = [world for world in bounded_client.call('unreal.world.list') if world.get('world_type') == 2]
                    if len(worlds) == 1:
                        bounded_client.call('unreal.console.execute', {'world': worlds[0]['object'], 'command': 'AuroraView.Demo.Scene'})
                        command_sent = True
                scene = demo_tools.find_scene(bounded_client, mode)
            if scene is None:
                time.sleep(min(0.2, startup.remaining()))
        result['world'], result['scene_object'] = scene
        if shared_tools:
            tools = demo_tools.DemoTools(client, scene[1], record,
                                         shared_tools=True, dispatcher=dispatcher)
        else:
            tools = demo_tools.DemoTools(client, scene[1], record)
        startup.remaining('tools')
        tools.register(registration_timeout=lambda: startup.remaining(maximum=5))
        startup.remaining()
        startup.remaining('view')
        bounded_client.call('auroraview.view.open', {
            'id': 'LiveDemo', 'title': 'AuroraView / Unreal ' + prepared['engine_version'] + ' / ' + mode.title(),
            'presentation': 'docked' if mode == 'editor' else 'floating',
            'html': html.read_text(encoding='utf-8')})
        if mode == 'editor':
            dock_editor_view(bounded_client, timeout=startup.remaining('dock', maximum=30))
        if not wait_for_browser(tools, process, timeout=startup.remaining('browser', maximum=45), dispatcher=dispatcher):
            raise build_plugin.BuildError('The real CEF dashboard did not finish its Core/Python handshake')
        presentation = bounded_client.call('auroraview.view.describe', {'id': 'LiveDemo'})
        if not presentation.get('ready') or (mode == 'editor' and (
                presentation.get('presentation') != 'docked' or not presentation.get('attached_to_root_window'))):
            raise build_plugin.BuildError('The native host did not confirm the requested view presentation')
        startup.remaining('ready')
        with lock:
            result['presentation'] = presentation
            result['status'] = 'running'
            save(receipt_path, result)
        print('Demo ready: Unreal ' + prepared['engine_version'] + ' ' + mode + ', PID ' + str(process.pid), flush=True)
        print('Use the dashboard: Lift cube / Reset scene / Python call / Python invoke / Send event.', flush=True)
        print('Close the host or press Ctrl+C here to stop. Session evidence: ' + str(receipt_path), flush=True)
        started = time.monotonic()
        while process.poll() is None:
            if (session_dir / 'stop.request').exists() or (session_seconds and time.monotonic() - started >= session_seconds):
                break
            if dispatcher:
                dispatcher.pump()
            time.sleep(0.2)
    except KeyboardInterrupt:
        with lock:
            result['interrupted'] = True
    except Exception as error:
        with lock:
            result.update(status='failed', error=str(error).replace(token, '<redacted>'))
        raise build_plugin.BuildError(result['error']) from None
    finally:
        cleanup_errors = []
        if tools:
            try:
                tools.close()
            except Exception as error:
                cleanup_errors.append('Tool cleanup: ' + str(error))
        if dispatcher:
            try:
                dispatcher.close()
            except Exception as error:
                cleanup_errors.append('Dispatcher cleanup: ' + str(error))
        if client:
            try:
                if process.poll() is None:
                    client.call('auroraview.host.shutdown')
            except (ConnectionClosedError, OSError, TimeoutError):
                pass
            except Exception as error:
                cleanup_errors.append('Host shutdown: ' + str(error))
            try:
                client.close()
            except Exception as error:
                cleanup_errors.append('Client cleanup: ' + str(error))
        if process:
            try:
                exit_code = process.wait(timeout=60)
                forced = False
            except subprocess.TimeoutExpired:
                forced = True
                try:
                    validate_game.stop_owned(process)
                except Exception as error:
                    cleanup_errors.append('Process cleanup: ' + str(error))
                exit_code = process.returncode
            except Exception as error:
                cleanup_errors.append('Host wait: ' + str(error))
                forced = process.poll() is None
                exit_code = process.returncode
            with lock:
                result.update(exit_code=exit_code, forced_cleanup=forced)
                if result['status'] != 'failed':
                    result['status'] = 'closed' if exit_code == 0 and not forced else 'failed'
        for path in [session_dir / 'Unreal.log', session_dir / 'console.log']:
            try:
                if path.is_file():
                    path.write_bytes(path.read_bytes().replace(token.encode('utf-8'), b'<redacted>'))
            except OSError as error:
                cleanup_errors.append('Log redaction: ' + str(error))
        with lock:
            if cleanup_errors:
                result.update(status='failed', cleanup_errors=[error.replace(token, '<redacted>') for error in cleanup_errors])
            result['completed_utc'] = now()
            save(receipt_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', type=Path, help='Installed UE root; source demos build automatically')
    parser.add_argument('--package', type=Path, help='Optional intact verified BuildPlugin Package directory')
    parser.add_argument('--mode', choices=('editor', 'game'), default='game')
    parser.add_argument('--bundle', type=Path, help='Extracted demo bundle; no Unreal installation required')
    parser.add_argument('--output', type=Path, required=True, help='New isolated output/run directory')
    parser.add_argument('--reuse', action='store_true', help='Reuse unchanged prepared build output')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--shared-tools', action='store_true',
                        help='Use the explicitly installed public auroraview-dcc-mcp 0.1.0 tool contracts')
    parser.add_argument('--timeout', type=int, default=2400)
    parser.add_argument('--startup-timeout', type=int, default=900,
                        help='Native endpoint/scene/view readiness budget, 30..3600 seconds; separate from build timeout')
    parser.add_argument('--session-seconds', type=int, default=0, help='Gracefully stop after N ready seconds; 0 keeps running')
    options = parser.parse_args()
    try:
        if os.name != 'nt':
            raise build_plugin.BuildError('The public native demo currently supports Win64 only')
        if options.timeout < 30 or options.session_seconds < 0:
            raise build_plugin.BuildError('Use timeout >=30 and session-seconds >=0')
        validate_startup_timeout(options.startup_timeout)
        protected = [ROOT]
        if options.bundle:
            if options.engine_root or options.package or options.reuse or options.mode != 'game':
                raise build_plugin.BuildError('--bundle is a Game launch and cannot combine with source build options')
            bundle = options.bundle.resolve()
            protected.append(bundle)
            prepared, html = bundle_inputs(bundle)
            output = isolated_output(options.output, protected)
            # Keep the downloaded artifact immutable and place Game Saved/logs
            # under this owned run, so subsequent launches verify the same ZIP.
            import shutil
            shutil.copytree(bundle / 'Game', output / 'Game')
            relative_executable = Path(prepared['executable']).relative_to(bundle)
            prepared['executable'] = str(output / relative_executable)
        else:
            if options.engine_root is None:
                raise build_plugin.BuildError('Supply --engine-root for a source demo or --bundle for a downloaded Game')
            engine = options.engine_root.resolve()
            protected.append(engine)
            if options.package:
                protected.append(options.package.resolve())
            output = isolated_output(options.output, protected, options.reuse)
            prepared = prepare(engine, options.package, output, options.mode, options.timeout, options.reuse)
            html = ROOT / 'Resources/live_demo.html'
        if options.prepare_only:
            print('Demo prepared: ' + str(output), flush=True)
            return 0
        result = launch(prepared, html, output, options.session_seconds, options.shared_tools, options.startup_timeout)
        return 0 if result['status'] == 'closed' else 1
    except (OSError, ValueError, build_plugin.BuildError) as error:
        print('Demo failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
