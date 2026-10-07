from pathlib import Path
import json
import unittest
ROOT=Path(__file__).resolve().parents[1]
class NativeSourceTests(unittest.TestCase):
    def test_published_core_assets_unchanged(self):
        import hashlib
        manifest=json.loads((ROOT/'ThirdParty/AuroraViewCore/manifest.json').read_text())
        for name,entry in manifest['files'].items():
            self.assertEqual(hashlib.sha256((ROOT/'ThirdParty/AuroraViewCore'/name).read_bytes()).hexdigest(),entry['sha256'])
    def test_real_native_dock_owns_browser(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewEditorModule.cpp').read_text()
        for marker in ('RegisterNomadTabSpawner','SNew(SDockTab)','DockTab->SetContent','TryInvokeTab','UnregisterNomadTabSpawner'):
            self.assertIn(marker,src)
        self.assertNotIn('SetParentDockTab',src)
    def test_private_layout_and_stable_identity(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewWorkspace.cpp').read_text()
        self.assertIn('NewTabManager',src);self.assertIn('FLayoutSaveRestore::SaveToConfig',src)
        self.assertNotIn('InstanceId',src);self.assertNotIn('GEditorLayoutIni',src)
    def test_callback_boundaries_copy_and_revalidate_owners(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewEditorModule.cpp').read_text()
        for marker in ('const TSharedPtr<FSession> Session = Entry ? *Entry', 'const auto Factory = Session->ContentFactory', 'Impl->IsCurrent(Id, Session)', 'Session->Browser == Browser', 'Session->TabEpoch == PresentationEpoch', 'const uint64 Request = ++Session->DockRequest', 'Sessions.GenerateValueArray(Snapshot)', 'NextPresentationGeneration'):
            self.assertIn(marker,src)
        stop=src[src.index('    void Stop()'):src.index('FAuroraViewReply FAuroraViewReply::Success')]
        self.assertLess(stop.index('Sessions.Reset()'),stop.index('Session->Dispose'))
        native=(ROOT/'Source/AuroraViewEditor/Private/Tests/AuroraViewDockReentrancyTests.cpp').read_text()
        for marker in ('I < 1024','Module.Close(CloseId)','Module.Remove(ReplaceId)','Module.Close(ReopenId)','Interrupted factories leave no orphan live tabs'):
            self.assertIn(marker,native)
    def test_error_tab_close_callback_precedes_browser_open(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewEditorModule.cpp').read_text()
        spawn=src[src.index('FOnSpawnTab::CreateLambda'):src.index('bool FAuroraViewEditorModule::OpenDocked')]
        self.assertLess(spawn.index('Session->OwnDockTab(Tab)'),spawn.index('OpenPresentation'))
        self.assertIn('Current->TabEpoch == OwnedEpoch',src)
        native=(ROOT/'Source/AuroraViewEditor/Private/Tests/AuroraViewDockRecoveryTests.cpp').read_text()
        self.assertIn('RetainedClosedTab->RequestCloseTab()',native)
        self.assertIn('NewTab != State->RetainedClosedTab',native)
if __name__=='__main__':unittest.main()
