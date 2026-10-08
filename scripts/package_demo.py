#!/usr/bin/env python3
"""Bundle a verified Development Game with the offline Python/demo launcher."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import zipfile

import build_plugin
import validate_game

ROOT = Path(__file__).resolve().parents[1]


def package_demo(game_run, output):
    game_run, output = Path(game_run).resolve(), Path(output).resolve()
    for path in [ROOT, game_run]:
        if build_plugin.inside(output, path) or build_plugin.inside(path, output):
            raise build_plugin.BuildError('Bundle output must be separate from source and Game inputs')
    if output.exists() or output.with_suffix('.zip').exists():
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
    output.mkdir(parents=True)
    (output / 'Game').mkdir()
    # Copy accepted build inputs only, not machine-specific runtime Saved files.
    for name in expected:
        path = output / 'Game' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive / name, path)
    for directory in ['python/auroraview_unreal', 'Resources']:
        shutil.copytree(ROOT / directory, output / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (output / 'scripts').mkdir()
    for name in ['run_demo.py', 'demo_tools.py', 'build_plugin.py', 'validate_game.py',
                 'preflight_engine.py', 'pe_evidence.py']:
        shutil.copy2(ROOT / 'scripts' / name, output / 'scripts' / name)
    shutil.copy2(ROOT / 'LICENSE', output / 'LICENSE')
    (output / 'README.txt').write_text(
        'AuroraView Unreal / native Development Game demo\n\n'
        'Requires Windows x64 and Python 3.9+. No Unreal installation, pip or npm needed.\n'
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
                    source={'commit': source['commit'], 'tree': source['tree'],
                            'working_files_sha256': source['working_files_sha256']},
                    executable='Game/' + candidates[0],
                    verification={'game_receipt_sha256': build_plugin.sha256(receipt_path),
                                  'native_scene': 'pass', 'normal_exit_code': 0,
                                  'rendered_browser': receipt.get('rendered_browser', 'not_run')},
                    created_utc=datetime.now(timezone.utc).isoformat(),
                    files_sha256=validate_game.inventory(output))
    (output / 'demo-package.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    zip_path = output.with_suffix('.zip')
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
