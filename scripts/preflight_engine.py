#!/usr/bin/env python3
"""Read-only engine inventory and build plan. Never installs, builds, or accepts terms."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADERS = [
    'Source/Runtime/WebBrowser/Public/SWebBrowser.h',
    'Source/Runtime/WebBrowser/Public/IWebBrowserWindow.h',
    'Source/Runtime/WebBrowser/Public/IWebBrowserSingleton.h',
    'Source/Runtime/Core/Public/Containers/Ticker.h',
    'Source/Runtime/CoreUObject/Public/UObject/StrongObjectPtr.h',
]

def inspect(engine_root):
    result = {'status':'blocked', 'unreal_compile':'not_run', 'unreal_ui':'not_run',
              'intended_source_target':'UE 5.7 Win64 Editor (experimental)', 'blockers':[]}
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
    if (version.get('MajorVersion'), version.get('MinorVersion')) != (5, 7):
        result['blockers'].append('Engine does not match the deliberately narrow 5.7 experimental source gate')
    result['header_sha256'] = {}
    for header in HEADERS:
        source = engine / header
        if source.is_file():
            result['header_sha256'][header] = hashlib.sha256(source.read_bytes()).hexdigest()
        else:
            result['blockers'].append('Missing public header: ' + header)
    for relative in ['Build/BatchFiles/RunUAT.bat', 'Binaries/Win64/UnrealEditor.exe']:
        if not (engine / relative).is_file():
            result['blockers'].append('Missing Win64 engine component: ' + relative)
    if not (engine / 'Binaries/ThirdParty/CEF3/Win64').is_dir():
        result['blockers'].append('CEF runtime folder requires verification for this installed engine')
    result['build_command_template'] = [str(engine / 'Build/BatchFiles/RunUAT.bat'), 'BuildPlugin',
        '-Plugin=' + str(ROOT / 'AuroraView.uplugin'), '-Package=<separate-output-directory>', '-TargetPlatforms=Win64']
    if not result['blockers']:
        result['status'] = 'inventory_pass_compile_pending'
    return result

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--engine-root')
    args=parser.parse_args()
    print(json.dumps(inspect(args.engine_root), indent=2))
