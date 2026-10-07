#!/usr/bin/env python3
"""Build the UE 5.7 Win64 Editor plugin and retain auditable native build evidence.

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
from datetime import datetime, timezone

import preflight_engine


ROOT = Path(__file__).resolve().parents[1]
BUILD_ENVIRONMENT = {
    'UnrealBuildTool_WindowsPlatform__CompilerVersion': 'Latest',
    'UnrealBuildTool_BuildConfiguration__bAllowUBAExecutor': 'false',
    'UnrealBuildTool_BuildConfiguration__MaxParallelActions': '1',
}
MODULE = 'AuroraViewEditor'
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
    expected = {'Name': MODULE, 'Type': 'Editor', 'LoadingPhase': 'PostEngineInit',
                'PlatformAllowList': ['Win64'], 'TargetAllowList': ['Editor']}
    if (descriptor.get('SupportedTargetPlatforms') != ['Win64'] or
            not isinstance(modules, list) or len(modules) != 1 or
            not isinstance(modules[0], dict) or
            any(modules[0].get(key) != value for key, value in expected.items())):
        raise BuildError(f'Descriptor must declare the UE 5.7 Win64 Editor module: {path}')
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


def compiler_evidence(log_path):
    text = log_path.read_text(encoding='utf-8-sig', errors='replace')
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
        raise BuildError('UAT log contains no actual Visual Studio compiler/toolchain evidence')
    return toolchains


def run_uat(command, source, log_path):
    if os.name != 'nt':
        raise BuildError('Native UE 5.7 Win64 compilation requires a Windows host')
    environment = os.environ.copy()
    environment.update(BUILD_ENVIRONMENT)
    with log_path.open('wb') as log:
        result = subprocess.run(command, cwd=source, env=environment,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    return result.returncode


def package_evidence(package, assets, engine_build_id):
    descriptor = validate_descriptor(package / 'AuroraView.uplugin')
    for relative, expected_hash in assets.items():
        path = package / relative
        if (not path.is_file() or path.is_symlink() or
                not inside(path.resolve(), package.resolve())):
            raise BuildError(f'Packaged asset is absent or unsafe: {relative}')
        if sha256(path) != expected_hash:
            raise BuildError(f'Packaged asset hash mismatch: {relative}')
    dll = package / 'Binaries/Win64' / DLL
    validate_win64_dll(dll)
    modules = read_json(package / 'Binaries/Win64/UnrealEditor.modules')
    if modules.get('Modules', {}).get(MODULE) != DLL:
        raise BuildError('Packaged UnrealEditor.modules does not map the AuroraViewEditor DLL')
    if modules.get('BuildId') != engine_build_id:
        raise BuildError('Packaged module BuildId does not match the installed engine')
    files = {}
    for path in sorted(package.rglob('*')):
        if path.is_symlink() or not inside(path.resolve(), package.resolve()):
            raise BuildError(f'Package contains an unsafe path: {path}')
        if path.is_file():
            files[path.relative_to(package).as_posix()] = sha256(path)
    return {'root': str(package), 'files_sha256': files,
            'dll': {'path': str(dll), 'sha256': sha256(dll), 'machine': 'AMD64', 'format': 'PE32+ DLL'},
            'descriptor': descriptor, 'modules': modules}


def build(engine_root, output, source_root=ROOT):
    source = Path(source_root).resolve()
    engine = Path(engine_root).resolve()
    output = prepare_output(output, source, engine)
    receipt_path = output / 'build-receipt.json'
    package = output / 'Package'
    log_path = output / 'uat.log'
    receipt = {'schema_version': 1, 'status': 'failed', 'unreal_compile': 'not_run',
               'unreal_ui': 'not_run', 'target': 'UE 5.7 Win64 Editor',
               'started_at_utc': datetime.now(timezone.utc).isoformat(),
               'environment_overrides': BUILD_ENVIRONMENT.copy(), 'errors': []}
    try:
        receipt['source'] = git_identity(source)
        receipt['preflight'] = preflight_engine.inspect(engine)
        if receipt['preflight'].get('blockers') or receipt['preflight'].get('status') != 'inventory_pass_compile_pending':
            raise BuildError('Engine preflight blocked: ' + '; '.join(receipt['preflight'].get('blockers', [])))
        installed = engine / 'Engine/Build/InstalledBuild.txt'
        if not installed.is_file():
            raise BuildError('Engine is not an installed build: Engine/Build/InstalledBuild.txt is absent')
        version_path = engine / 'Engine/Build/Build.version'
        version = read_json(version_path)
        if (version.get('MajorVersion'), version.get('MinorVersion')) != (5, 7):
            raise BuildError('Only the deliberately narrow UE 5.7 Win64 gate is supported')
        engine_modules_path = engine / 'Engine/Binaries/Win64/UnrealEditor.modules'
        engine_modules = read_json(engine_modules_path)
        build_id = engine_modules.get('BuildId')
        if not isinstance(build_id, str) or not build_id.strip():
            raise BuildError('Installed engine UnrealEditor.modules contains no BuildId')
        receipt['engine'] = {'root': str(engine), 'version': version, 'build_id': build_id,
                             'version_sha256': sha256(version_path),
                             'modules_sha256': sha256(engine_modules_path)}
        validate_descriptor(source / 'AuroraView.uplugin')
        assets = source_assets(source)
        receipt['source_assets_sha256'] = assets
        command = [str(engine / 'Engine/Build/BatchFiles/RunUAT.bat'), 'BuildPlugin',
                   '-Plugin=' + str(source / 'AuroraView.uplugin'), '-Package=' + str(package),
                   '-TargetPlatforms=Win64', '-StrictIncludes']
        receipt['build_command'] = command
        receipt['uat_log'] = str(log_path)
        print('Native build log: ' + str(log_path), flush=True)
        receipt['unreal_compile'] = 'failed'
        receipt['uat_exit_code'] = run_uat(command, source, log_path)
        if log_path.is_file():
            receipt['uat_log_sha256'] = sha256(log_path)
        if receipt['uat_exit_code'] != 0:
            raise BuildError(f'RunUAT failed with exit code {receipt["uat_exit_code"]}; see {log_path}')
        receipt['compiler_toolchains'] = compiler_evidence(log_path)
        receipt['package'] = package_evidence(package, assets, build_id)
        if git_identity(source) != receipt['source']:
            raise BuildError('Source Git identity or working files changed during the build')
        if (sha256(version_path) != receipt['engine']['version_sha256'] or
                sha256(engine_modules_path) != receipt['engine']['modules_sha256']):
            raise BuildError('Installed engine identity changed during the build')
        receipt['unreal_compile'] = 'pass'
        receipt['status'] = 'pass'
    except (BuildError, OSError, ValueError, TypeError, AttributeError) as error:
        receipt['errors'].append(str(error))
    finally:
        receipt['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-root', required=True, help='Installed UE 5.7 root containing Engine/')
    parser.add_argument('--output', required=True, help='New or empty run directory outside the source and engine trees')
    arguments = parser.parse_args()
    try:
        receipt = build(arguments.engine_root, arguments.output)
    except (BuildError, OSError) as error:
        print(json.dumps({'status': 'failed', 'unreal_compile': 'not_run',
                          'unreal_ui': 'not_run', 'errors': [str(error)]}, indent=2))
        return 1
    print(json.dumps({'status': receipt['status'], 'unreal_compile': receipt['unreal_compile'],
                      'unreal_ui': receipt['unreal_ui'], 'errors': receipt['errors'],
                      'receipt': str(Path(arguments.output).resolve() / 'build-receipt.json')}, indent=2))
    return 0 if receipt['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
