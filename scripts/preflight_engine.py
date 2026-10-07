#!/usr/bin/env python3
"""Read-only engine inventory and build plan. Never installs, builds, or accepts terms."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_VERSIONS = ('4.18', '4.26', '5.5', '5.7', '5.8')
HEADERS = [
    'Source/Runtime/WebBrowser/Public/SWebBrowser.h',
    'Source/Runtime/WebBrowser/Public/IWebBrowserWindow.h',
    'Source/Runtime/WebBrowser/Public/IWebBrowserSingleton.h',
    'Source/Runtime/Core/Public/Containers/Ticker.h',
    'Source/Runtime/CoreUObject/Public/UObject/StrongObjectPtr.h',
]

def engine_policy(version):
    """Explicit build candidates, never a claim of binary/runtime compatibility."""
    key = f'{version.get("MajorVersion")}.{version.get("MinorVersion")}'
    if key not in SUPPORTED_VERSIONS:
        raise ValueError('Engine is outside the explicit Win64 build matrix: ' + ', '.join(SUPPORTED_VERSIONS))
    return {'version': key, 'editor_target': 'UE4Editor' if key.startswith('4.') else 'UnrealEditor',
            'game_target': 'UE4Game' if key.startswith('4.') else 'UnrealGame',
            'strict_includes': key != '4.18', 'legacy_receipts': key == '4.18'}


def inspect(engine_root, expected_version=None):
    result = {'status':'blocked', 'unreal_compile':'not_run', 'unreal_ui':'not_run',
              'unreal_game_compile':'not_run', 'packaged_game':'not_run',
              'intended_source_target':'Win64 Editor and Runtime build candidates', 'blockers':[]}
    if not engine_root:
        result['blockers'].append('No actual Unreal Engine root was supplied or verified')
        return result
    engine = Path(engine_root).resolve() / 'Engine'
    version_path = engine / 'Build/Build.version'
    if not version_path.is_file():
        result['blockers'].append('Engine/Build/Build.version is absent')
        return result
    try:
        version = json.loads(version_path.read_text(encoding='utf-8-sig'))
        if not isinstance(version, dict):
            raise ValueError('version must be an object')
    except (OSError, ValueError) as error:
        result['blockers'].append('Cannot parse engine version: ' + str(error))
        return result
    result['engine_version'] = version
    try:
        policy = engine_policy(version)
    except ValueError as error:
        result['blockers'].append(str(error))
        return result
    result['engine_policy'] = policy
    if expected_version and policy['version'] != expected_version:
        result['blockers'].append(f'Engine version {policy["version"]} does not match requested {expected_version}')
    result['header_sha256'] = {}
    for header in HEADERS:
        source = engine / header
        if source.is_file():
            result['header_sha256'][header] = hashlib.sha256(source.read_bytes()).hexdigest()
        else:
            result['blockers'].append('Missing public header: ' + header)
    for relative in ['Build/BatchFiles/RunUAT.bat', f'Binaries/Win64/{policy["editor_target"]}.exe']:
        if not (engine / relative).is_file():
            result['blockers'].append('Missing Win64 engine component: ' + relative)
    if not (engine / 'Binaries/ThirdParty/CEF3/Win64').is_dir():
        result['blockers'].append('CEF runtime folder requires verification for this installed engine')
    result['build_command_template'] = [str(engine / 'Build/BatchFiles/RunUAT.bat'), 'BuildPlugin',
        '-Plugin=' + str(ROOT / 'AuroraView.uplugin'), '-Package=<separate-output-directory>',
        '-TargetPlatforms=Win64', '-NoDeleteHostProject']
    if policy['strict_includes']:
        result['build_command_template'].append('-StrictIncludes')
    if policy['version'] == '4.26':
        result['build_command_template'].append('-VS2019')
    if not result['blockers']:
        result['status'] = 'inventory_pass_compile_pending'
    return result

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine-root')
    parser.add_argument('--engine-version', choices=SUPPORTED_VERSIONS)
    args=parser.parse_args()
    print(json.dumps(inspect(args.engine_root, args.engine_version), indent=2))
