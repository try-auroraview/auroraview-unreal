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
import socket
import subprocess
import sys
import threading
import time

import build_plugin
import demo_tools
import preflight_engine
import validate_game

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from auroraview_unreal import Client, ConnectionClosedError


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)


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
    validate_game.create_project(project, package, version)
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
                    products_sha256=products, ubt_configuration_sha256=before_config,
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


def launch(prepared, html, output, session_seconds):
    session_dir = output / ('Session-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3))
    session_dir.mkdir()
    receipt_path = session_dir / 'session.json'
    token = secrets.token_urlsafe(32)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    mode, executable = prepared['mode'], Path(prepared['executable'])
    command = [str(executable)]
    if mode == 'editor':
        command += [prepared['uproject'], '/Engine/Maps/Entry']
    command += ['-AuroraViewDemo', '-AuroraViewAllowControl', '-AuroraViewHostPort=' + str(port),
                '-AuroraViewHostToken=' + token, '-NoSplash', '-NoSound', '-NoLiveCoding',
                '-Windowed', '-ResX=1000', '-ResY=720', '-d3d11', '-NoVSync',
                '-abslog=' + str(session_dir / 'Unreal.log'), '-stdout', '-FullStdOutLogOutput']
    result = dict(status='starting', mode=mode, started_utc=now(), source=prepared.get('source'),
                  engine_version=prepared['engine_version'], pid=None, port=port,
                  arguments=[arg.replace(token, '<redacted>') for arg in command[1:]],
                  browser_ready=False, events=[], exit_code=None, forced_cleanup=False)
    lock = threading.Lock()

    def record(kind, data):
        with lock:
            result['events'].append(dict(kind=kind, data=data, utc=now()))
            result['events'] = result['events'][-128:]
            if kind == 'browser.ready':
                result['browser_ready'] = True
            save(receipt_path, result)

    process = client = tools = None
    try:
        with (session_dir / 'console.log').open('wb') as stream:
            process = subprocess.Popen(command, cwd=executable.parent, stdout=stream, stderr=subprocess.STDOUT)
        result['pid'] = process.pid
        save(receipt_path, result)
        deadline = time.monotonic() + 180
        while client is None:
            if process.poll() is not None:
                raise build_plugin.BuildError('Demo host exited before native communication became ready')
            try:
                client = Client(port, token, expected_pid=process.pid, expected_context=mode,
                                expected_engine=prepared['engine_version'] + '.', timeout=5)
            except (OSError, TimeoutError):
                if time.monotonic() > deadline:
                    raise build_plugin.BuildError('Demo host did not start its authenticated control endpoint')
                time.sleep(0.2)
        result['identity'] = client.identity
        scene = None
        command_sent = False
        while scene is None:
            info = client.call('unreal.engine.info')
            if info.get('engine_ready'):
                if mode == 'editor' and not command_sent:
                    worlds = [world for world in client.call('unreal.world.list') if world.get('world_type') == 2]
                    if len(worlds) == 1:
                        client.call('unreal.console.execute', {'world': worlds[0]['object'], 'command': 'AuroraView.Demo.Scene'})
                        command_sent = True
                scene = demo_tools.find_scene(client, mode)
            if scene is None:
                if time.monotonic() > deadline:
                    raise build_plugin.BuildError('The owned demo scene did not become available')
                time.sleep(0.2)
        result['world'], result['scene_object'] = scene
        tools = demo_tools.DemoTools(client, scene[1], record)
        tools.register()
        client.call('auroraview.view.open', {
            'id': 'LiveDemo', 'title': 'AuroraView / Unreal ' + prepared['engine_version'] + ' / ' + mode.title(),
            'presentation': 'docked' if mode == 'editor' else 'floating',
            'html': html.read_text(encoding='utf-8')})
        if mode == 'editor':
            client.call('editor.view.dock', {'id': 'LiveDemo'})
        if not tools.browser_ready.wait(45):
            raise build_plugin.BuildError('The real CEF dashboard did not finish its Core/Python handshake')
        presentation = client.call('auroraview.view.describe', {'id': 'LiveDemo'})
        if not presentation.get('ready') or (mode == 'editor' and (
                presentation.get('presentation') != 'docked' or not presentation.get('attached_to_root_window'))):
            raise build_plugin.BuildError('The native host did not confirm the requested view presentation')
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
            time.sleep(0.2)
    except KeyboardInterrupt:
        with lock:
            result['interrupted'] = True
    except Exception as error:
        with lock:
            result.update(status='failed', error=str(error))
        raise
    finally:
        cleanup_errors = []
        if tools:
            try:
                tools.close()
            except Exception as error:
                cleanup_errors.append('Tool cleanup: ' + str(error))
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
                validate_game.stop_owned(process)
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
    parser.add_argument('--timeout', type=int, default=2400)
    parser.add_argument('--session-seconds', type=int, default=0, help='Gracefully stop after N ready seconds; 0 keeps running')
    options = parser.parse_args()
    try:
        if os.name != 'nt':
            raise build_plugin.BuildError('The public native demo currently supports Win64 only')
        if options.timeout < 30 or options.session_seconds < 0:
            raise build_plugin.BuildError('Use timeout >=30 and session-seconds >=0')
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
        result = launch(prepared, html, output, options.session_seconds)
        return 0 if result['status'] == 'closed' else 1
    except (OSError, ValueError, build_plugin.BuildError) as error:
        print('Demo failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
