#!/usr/bin/env python3
"""Bundle a verified Development Game with the offline Python/demo launcher."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import build_plugin
import preflight_engine
import validate_game

ROOT = Path(__file__).resolve().parents[1]
REDIST_PATH = 'Prerequisites/vc_redist.x64.exe'


def microsoft_installer(path):
    """Ask Windows to validate the publisher signature; never execute the installer."""
    if sys.platform != 'win32':
        raise build_plugin.BuildError('Microsoft prerequisite signature verification requires Windows')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = dict(os.environ, AURORAVIEW_REDIST_PATH=str(path))
    # Keep the file path out of PowerShell source so spaces and quotes are data.
    command = """
$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$signature = Get-AuthenticodeSignature -LiteralPath $env:AURORAVIEW_REDIST_PATH
$file = Get-Item -LiteralPath $env:AURORAVIEW_REDIST_PATH
[pscustomobject]@{
    status = [string]$signature.Status
    subject = [string]$signature.SignerCertificate.Subject
    thumbprint = [string]$signature.SignerCertificate.Thumbprint
    version = [string]$file.VersionInfo.ProductVersion
    product = [string]$file.VersionInfo.ProductName
    original_filename = [string]$file.VersionInfo.OriginalFilename
} | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                env=environment, capture_output=True, encoding='utf-8', timeout=120, check=True)
        signature = json.loads(result.stdout.lstrip('\ufeff').strip())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise build_plugin.BuildError('Unable to verify Microsoft prerequisite signature') from error
    if (signature.get('status') != 'Valid'
            or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', signature.get('subject', ''))
            or not re.fullmatch(r'[0-9A-Fa-f]{40}', signature.get('thumbprint', ''))
            or not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', signature.get('version', ''))
            or not re.match(r'^Microsoft Visual C\+\+ .*Redistributable \(x64\)', signature.get('product', ''))
            or signature.get('original_filename', '').lower() != 'vc_redist.x64.exe'):
        raise build_plugin.BuildError('Prerequisite must be a Microsoft-signed x64 VC++ installer with a valid version')
    return signature


def prerequisite(receipt):
    """Bind the redist to the same installed engine and actual logged MSVC family."""
    identity = receipt.get('engine', {})
    if not identity.get('root'):
        raise build_plugin.BuildError('Game receipt contains no installed engine root')
    engine = Path(identity['root']).resolve()
    version_path = engine / 'Engine/Build/Build.version'
    version = build_plugin.read_json(version_path)
    policy = preflight_engine.engine_policy(version)
    modules_path = engine / f'Engine/Binaries/Win64/{policy["editor_target"]}.modules'
    if (version != identity.get('version')
            or build_plugin.sha256(version_path) != identity.get('version_sha256')
            or build_plugin.sha256(modules_path) != identity.get('modules_sha256')
            or build_plugin.read_json(modules_path).get('BuildId') != identity.get('build_id')):
        raise build_plugin.BuildError('Installed engine changed after Game acceptance')
    versions = [entry.get('toolchain_version', '') for entry in receipt.get('compiler_toolchains', [])]
    if not versions or any(not re.fullmatch(r'\d+(?:\.\d+){2,3}', value) for value in versions):
        raise build_plugin.BuildError('Actual Game MSVC toolchain version evidence is required for prerequisites')
    # Compiler rebuild and redistributable package build numbers are different
    # version streams. Compare the MSVC family, retain the full observed values.
    family = max(tuple(int(part) for part in version.split('.')[:2]) for version in versions)
    installer = engine / 'Engine/Extras/Redist/en-us/vc_redist.x64.exe'
    if not installer.is_file() or installer.is_symlink():
        raise build_plugin.BuildError('Installed engine has no x64 VC++ redistributable installer')
    digest = build_plugin.sha256(installer)
    signature = microsoft_installer(installer)
    if build_plugin.sha256(installer) != digest:
        raise build_plugin.BuildError('Prerequisite changed during signature verification')
    if tuple(int(part) for part in signature['version'].split('.')[:2]) < family:
        raise build_plugin.BuildError('Microsoft prerequisite is older than the Game MSVC runtime family')
    return installer, dict(path=REDIST_PATH, sha256=digest,
                           publisher='Microsoft Corporation', authenticode_status='Valid',
                           signer_thumbprint=signature['thumbprint'], version=signature['version'],
                           product=signature['product'],
                           minimum_msvc_family='.'.join(str(part) for part in family),
                           observed_toolchain_versions=sorted(set(versions)), automatic_install=False)


def public_game_product(name):
    """Keep debug symbols and their staging index in the private build archive."""
    path = Path(name)
    return (path.suffix.lower() != '.pdb'
            and path.name.lower() != 'manifest_debugfiles_win64.txt')


def package_demo(game_run, output):
    game_run, output = Path(game_run).resolve(), Path(output).resolve()
    for path in [ROOT, game_run]:
        if build_plugin.inside(output, path) or build_plugin.inside(path, output):
            raise build_plugin.BuildError('Bundle output must be separate from source and Game inputs')
    zip_path = output.with_name(output.name + '.zip')
    if output.exists() or zip_path.exists():
        raise build_plugin.BuildError('Use a new demo bundle output')
    receipt_path = game_run / 'evidence/game-validation.json'
    receipt = build_plugin.read_json(receipt_path)
    if (receipt.get('status') != 'passed' or receipt.get('packaged_game') != 'pass'
            or receipt.get('runtime', {}).get('exit_code') != 0
            or receipt.get('runtime', {}).get('forced_cleanup') is not False
            or 'demo_scene' not in receipt.get('runtime', {}).get('actions', {})):
        raise build_plugin.BuildError('A passing native demo Game receipt with normal exit is required')
    source = build_plugin.git_identity(ROOT)
    if source['dirty'] or source != receipt.get('source'):
        raise build_plugin.BuildError('Bundle source must match the clean verified Game inputs')
    archive = game_run / 'PackagedGame'
    inventory = validate_game.inventory(archive)
    expected = receipt.get('stage', {}).get('files_sha256', {})
    if not expected or any(inventory.get(name) != digest for name, digest in expected.items()):
        raise build_plugin.BuildError('Staged Game changed after native acceptance')
    candidates = [name for name in expected if '/Binaries/Win64/' in '/' + name
                  and Path(name).name in [validate_game.PROJECT + '.exe', validate_game.PROJECT + '-Win64-Development.exe']]
    if len(candidates) != 1:
        raise build_plugin.BuildError('No unique receipt-bound Game executable')
    installer, redist = prerequisite(receipt)
    output.mkdir(parents=True)
    (output / 'Game').mkdir()
    # Copy accepted build inputs only, not machine-specific runtime Saved files.
    for name, digest in expected.items():
        if not public_game_product(name):
            continue
        path = output / 'Game' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive / name, path)
        if build_plugin.sha256(path) != digest:
            raise build_plugin.BuildError('Accepted Game product changed while copying the bundle')
    for directory in ['python/auroraview_unreal', 'Resources']:
        shutil.copytree(ROOT / directory, output / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (output / 'scripts').mkdir()
    for name in ['run_demo.py', 'demo_tools.py', 'build_plugin.py', 'validate_game.py',
                 'preflight_engine.py', 'pe_evidence.py']:
        shutil.copy2(ROOT / 'scripts' / name, output / 'scripts' / name)
    shutil.copy2(ROOT / 'LICENSE', output / 'LICENSE')
    (output / 'Prerequisites').mkdir()
    shutil.copy2(installer, output / REDIST_PATH)
    if build_plugin.sha256(output / REDIST_PATH) != redist['sha256']:
        raise build_plugin.BuildError('Microsoft prerequisite changed while copying the bundle')
    (output / 'README.txt').write_text(
        'AuroraView Unreal / native Development Game demo\n\n'
        'Requires Windows x64, Python 3.9+ and the Microsoft Visual C++ x64 runtime.\n'
        'No Unreal installation, compiler, pip or npm is needed.\n'
        'Before launching on a new Windows machine, install the included Microsoft-signed\n'
        f'  {REDIST_PATH} (version {redist["version"]})\n'
        'if that runtime or a newer compatible x64 runtime is not already installed.\n'
        f'The Game was compiled with MSVC family {redist["minimum_msvc_family"]}. Microsoft requires\n'
        'a redistributable at least as recent as the MSVC build tools used. Use a newer\n'
        'official Microsoft x64 redistributable if your system requires one:\n'
        '  https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist/\n'
        'The demo launcher never installs prerequisites automatically.\n\n'
        'From this folder run:\n'
        '  python scripts/run_demo.py --bundle . --output ..\\AuroraViewDemoRun\n\n'
        'Use a NEW output folder. Two real native windows open: scene and dashboard.\n'
        'Try Lift cube, Reset scene, Python call/invoke and Send event.\n'
        'Close the Game or press Ctrl+C in the launcher to exit normally.\n'
        'The control endpoint is authenticated, opt-in, loopback-only.\n'
        'This demo runs Development; Shipping is compiled separately.\n'
        'Project: https://github.com/try-auroraview/auroraview-unreal\n', encoding='utf-8')
    version = receipt['engine']['version']
    manifest = dict(schema_version=1, configuration='Development',
                    engine_version=f'{version["MajorVersion"]}.{version["MinorVersion"]}',
                    engine_build_id=receipt['engine']['build_id'],
                    engine_build_version_sha256=receipt['engine']['version_sha256'],
                    source={'commit': source['commit'], 'tree': source['tree'],
                            'working_files_sha256': source['working_files_sha256']},
                    executable='Game/' + candidates[0],
                    prerequisites={'vc_redist_x64': redist},
                    verification={'game_receipt_sha256': build_plugin.sha256(receipt_path),
                                  'native_scene': 'pass', 'normal_exit_code': 0,
                                  'rendered_browser': receipt.get('rendered_browser', 'not_run')},
                    created_utc=datetime.now(timezone.utc).isoformat(),
                    files_sha256=validate_game.inventory(output))
    (output / 'demo-package.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(output.rglob('*')):
            if path.is_file():
                bundle.write(path, output.name + '/' + path.relative_to(output).as_posix())
    return {'bundle': str(output), 'archive': str(zip_path), 'sha256': build_plugin.sha256(zip_path),
            'source_commit': source['commit'], 'engine_version': manifest['engine_version']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-run', required=True)
    parser.add_argument('--output', required=True)
    options = parser.parse_args()
    try:
        print(json.dumps(package_demo(options.game_run, options.output), indent=2))
        return 0
    except (OSError, ValueError, build_plugin.BuildError) as error:
        print('Bundle failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
