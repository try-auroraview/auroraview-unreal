#!/usr/bin/env python3
"""Build the explicit Unreal Win64 Editor/Runtime matrix with native evidence.

Uses only the installed engine and Python's standard library. A passing build
receipt proves compilation and package integrity, not Editor/UI acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import preflight_engine


ROOT = Path(__file__).resolve().parents[1]
BUILD_ENVIRONMENT = {
    'UnrealBuildTool_WindowsPlatform__CompilerVersion': 'Latest',
    'UnrealBuildTool_BuildConfiguration__bAllowUBAExecutor': 'false',
    'UnrealBuildTool_BuildConfiguration__MaxParallelActions': '1',
}
MODULE = 'AuroraViewEditor'
RUNTIME_MODULE = 'AuroraViewRuntime'
DLL = 'UnrealEditor-AuroraViewEditor.dll'


class BuildError(Exception):
    """A required build or evidence contract was not satisfied."""


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise BuildError(f'Cannot read JSON {path}: {error}') from error
    if not isinstance(value, dict):
        raise BuildError(f'Expected a JSON object: {path}')
    return value


def inside(path, parent):
    return path == parent or parent in path.parents


def prepare_output(output, source, engine):
    """Reserve an empty run directory; never remove an existing result."""
    output = Path(output).resolve()
    for name, protected in [('source', source), ('engine', engine)]:
        protected = protected.resolve()
        if inside(output, protected) or inside(protected, output):
            raise BuildError(f'Output must be isolated from the {name} tree: {output}')
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise BuildError(f'Output already exists and is not an empty directory: {output}')
    else:
        output.mkdir(parents=True)
    # Exclusive creation prevents two builds from claiming the same empty run.
    try:
        with (output / 'build-receipt.json').open('x', encoding='utf-8') as stream:
            stream.write('{"status":"in_progress","unreal_compile":"not_run","unreal_ui":"not_run"}\n')
    except FileExistsError as error:
        raise BuildError(f'Output is already reserved: {output}') from error
    return output


def git_identity(source):
    def git(*arguments):
        process = subprocess.run(['git', '-C', str(source), *arguments],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if process.returncode:
            raise BuildError('Cannot establish source Git identity: ' +
                             process.stderr.decode('utf-8', errors='replace').strip())
        return process.stdout

    top_name, commit, tree = git('rev-parse', '--show-toplevel', 'HEAD', 'HEAD^{tree}').decode('utf-8').splitlines()
    top = Path(top_name).resolve()
    if top != source.resolve():
        raise BuildError('Plugin source must be the root of its Git checkout')
    status = git('status', '--porcelain=v1', '-z', '--untracked-files=all')
    # Include untracked files so a dirty checkout is bound to the actual inputs.
    paths = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    files = {}
    for encoded in paths:
        if not encoded:
            continue
        relative = os.fsdecode(encoded)
        path = source / relative
        if path.is_symlink():
            raise BuildError(f'Source symlinks are not supported: {relative}')
        if path.is_file():
            files[Path(relative).as_posix()] = sha256(path)
    encoded_files = json.dumps(files, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {
        'root': str(source.resolve()), 'commit': commit, 'tree': tree,
        'dirty': bool(status), 'status_porcelain': os.fsdecode(status).replace('\0', '\n'),
        'working_files_sha256': hashlib.sha256(encoded_files).hexdigest(),
        'files': files,
    }


def tree_files(root, source):
    if not root.is_dir():
        raise BuildError(f'Required asset directory is absent: {root}')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not inside(path.resolve(), source.resolve()):
            raise BuildError(f'Asset must remain inside the source tree: {path}')
        if path.is_file():
            files[path.relative_to(source).as_posix()] = sha256(path)
    if not files:
        raise BuildError(f'Required asset directory is empty: {root}')
    return files


def source_assets(source):
    core = source / 'ThirdParty/AuroraViewCore'
    manifest = read_json(core / 'manifest.json')
    if not isinstance(manifest.get('files'), dict) or not manifest['files']:
        raise BuildError('Core manifest must pin at least one asset')
    for name, info in manifest['files'].items():
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or '..' in relative.parts or
                '\\' in name or ':' in name or not isinstance(info, dict)):
            raise BuildError(f'Invalid Core manifest asset path: {name}')
        path = core / name
        if not path.is_file() or path.is_symlink() or not inside(path.resolve(), core.resolve()):
            raise BuildError(f'Core manifest asset is absent or unsafe: {name}')
        if sha256(path) != info.get('sha256'):
            raise BuildError(f'Core manifest asset hash mismatch: {name}')
    try:
        if 'MIT License' not in (core / 'LICENSE').read_text(encoding='utf-8'):
            raise BuildError('Core MIT license is absent or invalid')
    except OSError as error:
        raise BuildError('Core MIT license is absent') from error
    files = tree_files(source / 'Resources', source)
    files.update(tree_files(core, source))
    if not (source / 'LICENSE').is_file():
        raise BuildError('Plugin license is absent')
    files['LICENSE'] = sha256(source / 'LICENSE')
    return files


def validate_descriptor(path):
    descriptor = read_json(path)
    modules = descriptor.get('Modules')
    if (descriptor.get('SupportedTargetPlatforms') != ['Win64'] or
            not isinstance(modules, list) or len(modules) != 2 or
            any(not isinstance(module, dict) for module in modules)):
        raise BuildError(f'Descriptor must declare both Win64 Runtime and Editor modules: {path}')
    by_name = {module.get('Name'): module for module in modules}
    for name, kind, phase in [(MODULE, 'Editor', 'PostEngineInit'), (RUNTIME_MODULE, 'Runtime', 'Default')]:
        module = by_name.get(name, {})
        platforms = module.get('PlatformAllowList', module.get('WhitelistPlatforms'))
        if module.get('Type') != kind or module.get('LoadingPhase') != phase or platforms != ['Win64']:
            raise BuildError(f'Descriptor must declare the Win64 {kind} module {name}: {path}')
    runtime_targets = by_name[RUNTIME_MODULE].get('TargetAllowList', by_name[RUNTIME_MODULE].get('WhitelistTargets'))
    if runtime_targets is not None and ('Game' not in runtime_targets or 'Editor' not in runtime_targets):
        raise BuildError('Runtime module must permit both Game and Editor targets')
    return descriptor


def validate_win64_dll(path):
    """Reject empty, renamed, non-PE, non-x64, and executable-only outputs."""
    try:
        with path.open('rb') as stream:
            header = stream.read(64)
            if len(header) < 64 or header[:2] != b'MZ':
                raise BuildError(f'Not a Windows PE DLL: {path}')
            pe_offset = struct.unpack_from('<I', header, 60)[0]
            if pe_offset < 64:
                raise BuildError(f'Invalid PE header offset: {path}')
            stream.seek(pe_offset)
            pe = stream.read(24)
            if len(pe) != 24 or pe[:4] != b'PE\0\0':
                raise BuildError(f'Invalid Windows PE signature: {path}')
            machine, sections = struct.unpack_from('<HH', pe, 4)
            optional_size, characteristics = struct.unpack_from('<HH', pe, 20)
            optional = stream.read(optional_size)
            if (machine != 0x8664 or not sections or not characteristics & 0x2000 or
                    optional_size < 112 or len(optional) != optional_size or
                    struct.unpack_from('<H', optional)[0] != 0x20B):
                raise BuildError(f'Output is not a Win64 DLL: {path}')
            section_headers = stream.read(40 * sections)
            if len(section_headers) != 40 * sections:
                raise BuildError(f'Truncated PE sections: {path}')
            file_size = path.stat().st_size
            for offset in range(0, len(section_headers), 40):
                raw_size, raw_pointer = struct.unpack_from('<II', section_headers, offset + 16)
                if raw_size and (not raw_pointer or raw_pointer + raw_size > file_size):
                    raise BuildError(f'Truncated PE section data: {path}')
    except OSError as error:
        raise BuildError(f'Required native DLL is absent: {path}') from error


def compiler_evidence(log_path, policy=None):
    logs = [log_path]
    child_logs = log_path.parent / 'uat-logs'
    if child_logs.is_dir():
        logs.extend(path for path in sorted(child_logs.rglob('*'))
                    if path.is_file() and path.suffix.lower() in ['.txt', '.log'])
    text = '\n'.join(path.read_text(encoding='utf-8-sig', errors='replace') for path in logs)
    toolchains = []
    pattern = re.compile(r'Using\s+(Visual Studio\s+\d+)\s+(\d+(?:\.\d+)+)\s+toolchain', re.IGNORECASE)
    sdk_pattern = re.compile(r'Windows\s+(\d+(?:\.\d+)+)\s+SDK', re.IGNORECASE)

    def parenthesized_path(tail):
        # Real Windows SDK/toolchain paths can contain "Program Files (x86)".
        tail = tail.lstrip()
        if not tail.startswith('('):
            return None
        depth = 0
        for offset, character in enumerate(tail):
            if character == '(':
                depth += 1
            elif character == ')':
                depth -= 1
                if depth == 0:
                    return tail[1:offset]
        return None

    for line in text.splitlines():
        match = pattern.search(line)
        if match:
            item = {'compiler': match.group(1), 'toolchain_version': match.group(2),
                    'toolchain_path': parenthesized_path(line[match.end():]), 'log_line': line}
            sdk = sdk_pattern.search(line)
            if sdk:
                item.update({'windows_sdk_version': sdk.group(1),
                             'windows_sdk_path': parenthesized_path(line[sdk.end():])})
            if item not in toolchains:
                toolchains.append(item)
    if not toolchains:
        if policy and policy['legacy_receipts']:
            # UE 4.18's default UBT spew does not identify the compiler. Do not
            # turn a configured compiler or installed executable into used-toolchain evidence.
            return []
        raise BuildError('UAT log contains no actual Visual Studio compiler/toolchain evidence')
    return toolchains


def build_environment(policy, output):
    overrides = {'uebp_LogFolder': str(output / 'uat-logs')}
    if not policy['version'].startswith('4.'):
        overrides.update(BUILD_ENVIRONMENT)
    if policy['version'] == '5.5':
        overrides['UBT_EXTRA_ARGS'] = '-NoUBA -NoUBALocal -MaxParallelActions=1'
    return overrides


def run_uat(command, source, log_path, environment_overrides):
    if os.name != 'nt':
        raise BuildError('Native Win64 compilation requires a Windows host')
    environment = os.environ.copy()
    environment.update(environment_overrides)
    with log_path.open('wb') as log:
        result = subprocess.run(command, cwd=source, env=environment,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    return result.returncode


def package_evidence(package, assets, engine_build_id, policy):
    descriptor = validate_descriptor(package / 'AuroraView.uplugin')
    for relative, expected_hash in assets.items():
        path = package / relative
        if (not path.is_file() or path.is_symlink() or
                not inside(path.resolve(), package.resolve())):
            raise BuildError(f'Packaged asset is absent or unsafe: {relative}')
        if sha256(path) != expected_hash:
            raise BuildError(f'Packaged asset hash mismatch: {relative}')
    editor = policy['editor_target']
    modules = read_json(package / f'Binaries/Win64/{editor}.modules')
    if modules.get('BuildId') != engine_build_id:
        raise BuildError('Packaged module BuildId does not match the installed engine')
    binaries = {}
    for name in [MODULE, RUNTIME_MODULE]:
        dll_name = f'{editor}-{name}.dll'
        dll = package / 'Binaries/Win64' / dll_name
        validate_win64_dll(dll)
        if modules.get('Modules', {}).get(name) != dll_name:
            raise BuildError(f'Packaged {editor}.modules does not map the {name} DLL')
        binaries[name] = {'path': str(dll), 'sha256': sha256(dll), 'machine': 'AMD64', 'format': 'PE32+ DLL'}
    files = {}
    for path in sorted(package.rglob('*')):
        if path.is_symlink() or not inside(path.resolve(), package.resolve()):
            raise BuildError(f'Package contains an unsafe path: {path}')
        if path.is_file():
            files[path.relative_to(package).as_posix()] = sha256(path)
    return {'root': str(package), 'files_sha256': files,
            'dll': binaries[MODULE], 'editor_binaries': binaries,
            'descriptor': descriptor, 'modules': modules}


def configuration_inputs(engine):
    """Record existing UBT configuration without changing engine/user files."""
    directories = [engine / 'Engine/Saved/UnrealBuildTool',
                   engine / 'Engine/Programs/NotForLicensees/UnrealBuildTool',
                   engine / 'Engine/Restricted/NotForLicensees/Programs/UnrealBuildTool']
    if os.name == 'nt':
        import ctypes
        for folder_id in [0x1A, 0x05]:  # Actual .NET ApplicationData / Personal locations.
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None, folder_id, None, 0, buffer) == 0 and buffer.value:
                directories.append(Path(buffer.value) / 'Unreal Engine/UnrealBuildTool')
    files = {}
    for directory in directories:
        path = directory / 'BuildConfiguration.xml'
        if path.is_file():
            files[str(path.resolve())] = sha256(path)
    return files


def validate_native_static(path):
    if path.suffix.lower() == '.lib':
        native_members = 0
        with path.open('rb') as stream:
            if stream.read(8) != b'!<arch>\n':
                raise BuildError(f'Not a native COFF static library: {path}')
            while stream.tell() < path.stat().st_size:
                member = stream.read(60)
                if len(member) != 60 or member[-2:] != b'`\n':
                    raise BuildError(f'Invalid COFF archive member: {path}')
                try:
                    size = int(member[48:58].strip())
                except ValueError as error:
                    raise BuildError(f'Invalid COFF archive member size: {path}') from error
                if size < 0 or stream.tell() + size > path.stat().st_size:
                    raise BuildError(f'Truncated COFF archive member: {path}')
                if member[:16].strip() not in [b'/', b'//', b'/SYM64/']:
                    header = stream.read(min(size, 20))
                    validate_coff_header(header, path)
                    native_members += 1
                    stream.seek(size - len(header), 1)
                else:
                    stream.seek(size, 1)
                if size % 2:
                    stream.seek(1, 1)
        if not native_members:
            raise BuildError(f'COFF static library has no native Win64 members: {path}')
    elif path.suffix.lower() == '.obj':
        with path.open('rb') as stream:
            validate_coff_header(stream.read(20), path)
    else:
        raise BuildError(f'Unexpected Runtime native build product: {path}')


def validate_coff_header(header, path):
    machine = struct.unpack_from('<H', header)[0] if len(header) >= 20 else 0
    if header[:4] == b'\0\0\xff\xff' and len(header) >= 20:
        machine = struct.unpack_from('<H', header, 6)[0]
    if machine != 0x8664:
        raise BuildError(f'Not a native Win64 COFF object: {path}')


def game_evidence(package, host, policy):
    """Prove UBT Game Development/Shipping products; this does not cook or run."""
    plugin = host / 'Plugins/AuroraView'
    original_plugin = package / 'HostProject/Plugins/AuroraView'
    results = []
    for configuration in ['Development', 'Shipping']:
        if policy['legacy_receipts']:
            receipt_name = 'UE4Game.target' if configuration == 'Development' else 'UE4Game-Win64-Shipping.target'
            manifest = plugin / 'Binaries/Win64' / receipt_name
            data = read_json(manifest)
            if (data.get('TargetName') != policy['game_target'] or data.get('Platform') != 'Win64' or
                    data.get('Configuration') != configuration):
                raise BuildError(f'Game target receipt identity mismatch: {manifest}')
            products = [item['Path'].replace('$(ProjectDir)', str(package / 'HostProject'))
                        for item in data.get('BuildProducts', []) if isinstance(item, dict) and 'Path' in item]
        else:
            manifest = host / f'Saved/Manifest-{policy["game_target"]}-Win64-{configuration}.xml'
            try:
                tree = ET.parse(manifest)
            except (OSError, ET.ParseError) as error:
                raise BuildError(f'Cannot read actual Game build manifest {manifest}: {error}') from error
            products = [element.text for element in tree.findall('.//BuildProducts/string') if element.text]
        runtime_products = {}
        reusable_product = False
        for product in products:
            path = Path(product).resolve()
            if RUNTIME_MODULE.lower() not in product.lower() or not inside(path, original_plugin.resolve()):
                continue
            relative = path.relative_to(original_plugin.resolve())
            if path.suffix.lower() not in ['.lib', '.obj', '.precompiled']:
                continue
            if path.suffix.lower() in ['.obj', '.precompiled'] and (configuration not in relative.parts or
                                                                   policy['game_target'] not in relative.parts):
                raise BuildError(f'Runtime build product does not belong to the requested Game target: {relative}')
            packaged = package / relative
            produced = plugin / relative
            if not produced.is_file() or not packaged.is_file() or sha256(produced) != sha256(packaged):
                raise BuildError(f'Game Runtime build product is absent or changed in package: {relative}')
            if path.suffix.lower() == '.precompiled':
                outputs = read_json(produced).get('OutputFiles')
                if not isinstance(outputs, list) or not outputs:
                    raise BuildError(f'Runtime precompiled manifest has no native outputs: {relative}')
                for output_name in outputs:
                    object_path = (packaged.parent / output_name).resolve()
                    produced_object = (produced.parent / output_name).resolve()
                    if (not inside(object_path, package.resolve()) or not inside(produced_object, plugin.resolve()) or
                            not object_path.is_file() or not produced_object.is_file() or
                            sha256(object_path) != sha256(produced_object)):
                        raise BuildError(f'Runtime precompiled object is absent, changed or unsafe: {output_name}')
                    validate_native_static(object_path)
                    runtime_products[object_path.relative_to(package).as_posix()] = sha256(object_path)
                reusable_product = True
            else:
                validate_native_static(packaged)
                reusable_product |= path.suffix.lower() == '.lib'
            runtime_products[relative.as_posix()] = sha256(packaged)
        if not runtime_products:
            raise BuildError(f'No actual {configuration} Game Runtime native products are recorded')
        if not reusable_product:
            raise BuildError(f'No packaged Runtime library or precompiled manifest for {configuration}')
        results.append({'target': policy['game_target'], 'configuration': configuration, 'platform': 'Win64',
                        'manifest': str(manifest), 'manifest_sha256': sha256(manifest),
                        'products_sha256': runtime_products})
    return results


def build(engine_root, output, source_root=ROOT, expected_version=None, require_clean=False):
    source = Path(source_root).resolve()
    engine = Path(engine_root).resolve()
    output = prepare_output(output, source, engine)
    receipt_path = output / 'build-receipt.json'
    package = output / 'Package'
    log_path = output / 'uat.log'
    receipt = {'schema_version': 2, 'status': 'failed', 'unreal_compile': 'not_run',
               'unreal_game_compile': 'not_run', 'packaged_game': 'not_run',
               'unreal_ui': 'not_run', 'target': 'Win64 Editor and Runtime',
               'started_at_utc': datetime.now(timezone.utc).isoformat(),
               'errors': []}
    try:
        receipt['source'] = git_identity(source)
        if require_clean and receipt['source']['dirty']:
            raise BuildError('A clean source checkout is required for this build')
        receipt['preflight'] = preflight_engine.inspect(engine, expected_version)
        if receipt['preflight'].get('blockers') or receipt['preflight'].get('status') != 'inventory_pass_compile_pending':
            raise BuildError('Engine preflight blocked: ' + '; '.join(receipt['preflight'].get('blockers', [])))
        installed = engine / 'Engine/Build/InstalledBuild.txt'
        if not installed.is_file():
            raise BuildError('Engine is not an installed build: Engine/Build/InstalledBuild.txt is absent')
        version_path = engine / 'Engine/Build/Build.version'
        version = read_json(version_path)
        policy = preflight_engine.engine_policy(version)
        receipt['target'] = f'UE {policy["version"]} Win64 Editor and Runtime'
        receipt['environment_overrides'] = build_environment(policy, output)
        receipt['ubt_configuration_sha256'] = configuration_inputs(engine)
        engine_modules_path = engine / f'Engine/Binaries/Win64/{policy["editor_target"]}.modules'
        engine_modules = read_json(engine_modules_path)
        build_id = engine_modules.get('BuildId')
        if not isinstance(build_id, str) or not build_id.strip():
            raise BuildError('Installed engine module manifest contains no BuildId')
        receipt['engine'] = {'root': str(engine), 'version': version, 'build_id': build_id,
                             'version_sha256': sha256(version_path),
                             'modules_sha256': sha256(engine_modules_path)}
        validate_descriptor(source / 'AuroraView.uplugin')
        assets = source_assets(source)
        receipt['source_assets_sha256'] = assets
        command = [str(engine / 'Engine/Build/BatchFiles/RunUAT.bat'), 'BuildPlugin',
                   '-Plugin=' + str(source / 'AuroraView.uplugin'), '-Package=' + str(package),
                   '-TargetPlatforms=Win64', '-NoDeleteHostProject']
        if policy['strict_includes']:
            command.append('-StrictIncludes')
        if policy['version'] == '4.26':
            command.append('-VS2019')
        if policy['version'].startswith('4.') and (engine / 'Engine/Binaries/DotNET/AutomationTool.exe').is_file():
            command.append('-nocompile')
        receipt['build_command'] = command
        receipt['uat_log'] = str(log_path)
        print('Native build log: ' + str(log_path), flush=True)
        receipt['unreal_compile'] = 'failed'
        receipt['unreal_game_compile'] = 'failed'
        receipt['uat_exit_code'] = run_uat(command, source, log_path, receipt['environment_overrides'])
        if log_path.is_file():
            receipt['uat_log_sha256'] = sha256(log_path)
        if receipt['uat_exit_code'] != 0:
            raise BuildError(f'RunUAT failed with exit code {receipt["uat_exit_code"]}; see {log_path}')
        receipt['compiler_toolchains'] = compiler_evidence(log_path, policy)
        receipt['compiler_evidence_status'] = 'recorded' if receipt['compiler_toolchains'] else 'not_logged_by_legacy_ubt'
        host = output / 'HostProject'
        if not (package / 'HostProject').is_dir():
            raise BuildError('UAT did not retain its actual HostProject build evidence')
        (package / 'HostProject').rename(host)
        receipt['package'] = package_evidence(package, assets, build_id, policy)
        receipt['game_targets'] = game_evidence(package, host, policy)
        receipt['uat_child_logs_sha256'] = {
            path.relative_to(output).as_posix(): sha256(path)
            for path in (output / 'uat-logs').rglob('*') if path.is_file()}
        configuration_after = configuration_inputs(engine)
        receipt['ubt_configuration_after_sha256'] = configuration_after
        if any(configuration_after.get(path) != digest for path, digest in receipt['ubt_configuration_sha256'].items()):
            raise BuildError('An existing UBT configuration changed during the build')
        if git_identity(source) != receipt['source']:
            raise BuildError('Source Git identity or working files changed during the build')
        if (sha256(version_path) != receipt['engine']['version_sha256'] or
                sha256(engine_modules_path) != receipt['engine']['modules_sha256']):
            raise BuildError('Installed engine identity changed during the build')
        receipt['unreal_compile'] = 'pass'
        receipt['unreal_game_compile'] = 'pass'
        receipt['status'] = 'pass'
    except (BuildError, OSError, ValueError, TypeError, AttributeError) as error:
        receipt['errors'].append(str(error))
    finally:
        receipt['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', required=True, help='Installed engine root containing Engine/')
    parser.add_argument('--engine-version', choices=preflight_engine.SUPPORTED_VERSIONS)
    parser.add_argument('--require-clean', action='store_true')
    parser.add_argument('--output', required=True, help='New or empty run directory outside the source and engine trees')
    arguments = parser.parse_args()
    try:
        receipt = build(arguments.engine_root, arguments.output, expected_version=arguments.engine_version,
                        require_clean=arguments.require_clean)
    except (BuildError, OSError) as error:
        print(json.dumps({'status': 'failed', 'unreal_compile': 'not_run',
                          'unreal_ui': 'not_run', 'errors': [str(error)]}, indent=2))
        return 1
    print(json.dumps({'status': receipt['status'], 'unreal_compile': receipt['unreal_compile'],
                      'unreal_game_compile': receipt['unreal_game_compile'], 'packaged_game': receipt['packaged_game'],
                      'unreal_ui': receipt['unreal_ui'], 'errors': receipt['errors'],
                      'receipt': str(Path(arguments.output).resolve() / 'build-receipt.json')}, indent=2))
    return 0 if receipt['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
