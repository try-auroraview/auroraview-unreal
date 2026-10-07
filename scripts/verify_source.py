#!/usr/bin/env python3
"""Executable source/asset invariants. These do not compile or validate Unreal."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    checks = []
    manifest = json.loads((ROOT / 'ThirdParty/AuroraViewCore/manifest.json').read_text())
    for name, info in manifest['files'].items():
        assert hashlib.sha256((ROOT / 'ThirdParty/AuroraViewCore' / name).read_bytes()).hexdigest() == info['sha256'], name
        checks.append('pinned_' + name)
    assert 'MIT License' in (ROOT / 'ThirdParty/AuroraViewCore/LICENSE').read_text()
    checks.append('upstream_license_retained')
    descriptor = json.loads((ROOT / 'AuroraView.uplugin').read_text())
    assert descriptor['SupportedTargetPlatforms'] == ['Win64']
    assert descriptor['Modules'][0]['Type'] == 'Editor'
    checks.append('editor_win64_boundary')
    build = (ROOT / 'Source/AuroraViewEditor/AuroraViewEditor.Build.cs').read_text()
    for dependency in ['Core', 'CoreUObject', 'Json', 'UnrealEd', 'Slate', 'SlateCore', 'WebBrowser', 'Projects']:
        assert '"' + dependency + '"' in build
    assert 'Target.Version.MinorVersion != 7' in build
    checks.append('build_dependencies_and_source_version_gate')
    module = (ROOT / 'Source/AuroraViewEditor/Private/AuroraViewEditorModule.cpp').read_text()
    for marker in ['CloseBrowser(true, false)', 'UnbindUObject(', 'RemoveTicker(',
                   'OnEnginePreExit.Remove(', 'Mailbox->Stop()', '__auroraview_call_result',
                   'check(IsInGameThread())', 'Request.bIsMainFrame', 'NavigationUsed->exchange(true)',
                   'frame-src \'none\'', 'connect-src \'none\'']:
        assert marker in module, marker
    assert 'AsyncTask(' not in module
    checks.append('native_lifecycle_and_navigation_source_guards')
    endpoint = (ROOT / 'Source/AuroraViewEditor/Private/AuroraViewEndpoint.cpp').read_text()
    assert 'Mailbox->Push(Generation' in endpoint and 'Token != SessionToken' in endpoint
    assert 'Mailbox->PushControl(Generation, AuroraView::SessionMailbox::Kind::Ready)' in endpoint
    checks.append('enqueue_only_native_endpoint')
    assert 'bUsableId' in module and 'TEXT("INVALID_REQUEST")' in module
    assert module.index('const bool bUsableId') < module.index('TryGetStringField(TEXT("type")')
    checks.append('usable_id_invalid_type_error_source_guard')
    sources = '\n'.join(p.read_text() for p in (ROOT / 'Source').rglob('*') if p.is_file())
    for prohibited in ['Python.h', 'PySide', 'GetForegroundWindow', 'SetParent(', 'system(', 'CreateProcess(']:
        assert prohibited not in sources, prohibited
    checks.append('no_python_qt_foreground_hwnd_or_shell_backend')
    print(json.dumps({'status':'pass','checks':checks,'unreal_compile':'not_run','unreal_ui':'not_run'}, indent=2))

if __name__ == '__main__':
    main()
