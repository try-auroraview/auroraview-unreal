#!/usr/bin/env python3
"""Executable source/asset invariants. These do not compile or validate Unreal."""
import hashlib
import json
from pathlib import Path
from build_legacy_bridge import check as check_legacy_bridge

ROOT = Path(__file__).resolve().parents[1]

def main():
    checks = []
    manifest = json.loads((ROOT / 'ThirdParty/AuroraViewCore/manifest.json').read_text())
    for name, info in manifest['files'].items():
        assert hashlib.sha256((ROOT / 'ThirdParty/AuroraViewCore' / name).read_bytes()).hexdigest() == info['sha256'], name
        checks.append('pinned_' + name)
    assert 'MIT License' in (ROOT / 'ThirdParty/AuroraViewCore/LICENSE').read_text()
    checks.append('upstream_license_retained')
    check_legacy_bridge()
    checks.append('chrome59_bridge_provenance_and_hashes')
    descriptor = json.loads((ROOT / 'AuroraView.uplugin').read_text())
    assert descriptor['SupportedTargetPlatforms'] == ['Win64']
    assert [(item['Name'], item['Type']) for item in descriptor['Modules']] == [
        ('AuroraViewRuntime', 'Runtime'), ('AuroraViewEditor', 'Editor')]
    checks.append('runtime_editor_win64_boundary')
    editor_build = (ROOT / 'Source/AuroraViewEditor/AuroraViewEditor.Build.cs').read_text()
    runtime_build = (ROOT / 'Source/AuroraViewRuntime/AuroraViewRuntime.Build.cs').read_text()
    for dependency in ['Core', 'CoreUObject', 'Json', 'Slate', 'SlateCore', 'WebBrowser', 'Projects']:
        assert '"' + dependency + '"' in runtime_build
    assert '"UnrealEd"' in editor_build and '"AuroraViewRuntime"' in editor_build
    for dependency in ['UnrealEd', 'ContentBrowser', 'SceneOutliner']:
        assert '"' + dependency + '"' not in runtime_build
    compatibility = (ROOT / 'Source/AuroraViewRuntime/Public/AuroraViewCompatibility.h').read_text()
    for marker in ['!PLATFORM_WINDOWS', 'ENGINE_MAJOR_VERSION == 4', 'ENGINE_MINOR_VERSION == 18',
                   'ENGINE_MINOR_VERSION == 26', 'ENGINE_MAJOR_VERSION == 5',
                   'ENGINE_MINOR_VERSION == 5', 'ENGINE_MINOR_VERSION == 7', 'ENGINE_MINOR_VERSION == 8',
                   '#error AuroraView supports']:
        assert marker in compatibility, marker
    checks.append('build_dependencies_and_explicit_version_gate')
    module = (ROOT / 'Source/AuroraViewRuntime/Private/AuroraViewRuntimeModule.cpp').read_text()
    for marker in ['CloseBrowser(true, false)', 'UnbindUObject(', 'RemoveTicker(',
                   'PreExit().Remove(', 'Mailbox->Stop()', '__auroraview_call_result',
                   'check(IsInGameThread())', 'Request.bIsMainFrame', 'Request.bIsRedirect',
                   'Mailbox->IsCurrent(Epoch)', 'DocumentStartup->AllowNavigation(',
                   'frame-src \'none\'', 'connect-src \'none\'']:
        assert marker in module, marker
    assert 'AsyncTask(' not in module
    checks.append('native_lifecycle_and_navigation_source_guards')
    startup = (ROOT / 'Source/AuroraViewRuntime/Private/BrowserDocumentStartup.h').read_text()
    assert 'class BrowserDocumentStartup final' in startup
    assert '#include "BrowserDocumentStartup.h"' in module
    load = module[module.index('    void LoadPendingDocument()'):module.index('    void Emit(')]
    assert load.index('Mailbox->IsCurrent(Generation)') < load.index('BeginOwnedDocument(') < load.index('->LoadString(')
    assert 'BrowserToLoad->GetUrl() == TEXT("about:blank") && BrowserToLoad->IsLoaded()' in load
    assert load.index('FPlatformTime::Seconds() > ReadyDeadline') < load.index('BeginOwnedDocument(')
    assert module.count('->LoadString(') == 1
    assert 'Session->ReadyDeadline = FPlatformTime::Seconds() + 10.0;' in module
    dispose = module[module.index('    void Dispose('):module.index('    void Pump()')]
    assert dispose.index('DocumentStartup->Close()') < dispose.index('MoveTemp(Browser)')
    assert dispose.index('PendingDocument.Empty()') < dispose.index('MoveTemp(Browser)')
    checks.append('single_owned_document_after_initial_frame_source_guard')
    endpoint = (ROOT / 'Source/AuroraViewRuntime/Private/AuroraViewEndpoint.cpp').read_text()
    assert 'Mailbox->Push(Generation' in endpoint and 'Token != SessionToken' in endpoint
    assert 'Mailbox->PushControl(Generation, AuroraView::SessionMailbox::Kind::Ready)' in endpoint
    checks.append('enqueue_only_native_endpoint')
    assert 'bUsableId' in module and 'TEXT("INVALID_REQUEST")' in module
    assert module.index('const bool bUsableId') < module.index('Values.Find(TEXT("type"))')
    assert '(*TypeValue)->Type == EJson::String' in module
    checks.append('usable_id_invalid_type_error_source_guard')
    sources = '\n'.join(p.read_text() for p in (ROOT / 'Source').rglob('*') if p.is_file())
    for prohibited in ['Python.h', 'PySide', 'GetForegroundWindow', 'SetParent(', 'system(', 'CreateProcess(']:
        assert prohibited not in sources, prohibited
    checks.append('no_python_qt_foreground_hwnd_or_shell_backend')
    print(json.dumps({'status':'pass','checks':checks,'unreal_compile':'not_run','unreal_ui':'not_run'}, indent=2))

if __name__ == '__main__':
    main()
