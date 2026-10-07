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
        src=(ROOT/'Source/AuroraViewRuntime/Private/AuroraViewRuntimeModule.cpp').read_text()
        for marker in ('RegisterNomadTabSpawner','SNew(SDockTab)','DockTab->SetContent','TryInvokeTab','UnregisterNomadTabSpawner'):
            self.assertIn(marker,src)
        self.assertNotIn('SetParentDockTab',src)
    def test_private_layout_and_stable_identity(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewWorkspace.cpp').read_text()
        self.assertIn('NewTabManager',src);self.assertIn('FLayoutSaveRestore::SaveToConfig',src)
        self.assertNotIn('InstanceId',src);self.assertNotIn('GEditorLayoutIni',src)
    def test_no_html_native_drag_simulation(self):
        html=(ROOT/'Resources/native_showcase.html').read_text()
        for marker in ('dataTransfer','dragstart','eval(','new Function('):self.assertNotIn(marker,html)
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeDrag.cpp').read_text()
        for marker in ('OnDragDetected','BeginDragDrop','FActorDragDropGraphEdOp::New','FAssetDragDropOp::New','OnDrop'):
            self.assertIn(marker,src)
    def test_guarded_fixture_and_weak_scope(self):
        fixture=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewFixture.cpp').read_text()
        for marker in ('AuroraViewNativeFixture','AuroraViewAllowFixtureMutations','PlayWorld','/Engine/'):
            self.assertIn(marker,fixture)
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeShowcase.cpp').read_text()
        for marker in ('FGuid::NewGuid','ActorsById.Reset','FScopedTransaction','CONFLICT','Transaction.Cancel','selectionMatched'):
            self.assertIn(marker,src)
        for marker in ('LoadObject<','FindObject<','GetPathName()'):self.assertNotIn(marker,src)
    def test_package_preserves_bridge_assets(self):
        config=(ROOT/'Config/FilterPlugin.ini').read_text()
        self.assertIn('/ThirdParty/AuroraViewCore/...',config)
        self.assertIn('/LICENSE',config)
        build=(ROOT/'Source/AuroraViewRuntime/AuroraViewRuntime.Build.cs').read_text()
        self.assertIn('Resources/native_showcase.html',build)
    def test_callback_boundaries_copy_and_revalidate_owners(self):
        src=(ROOT/'Source/AuroraViewRuntime/Private/AuroraViewRuntimeModule.cpp').read_text()
        for marker in ('const TSharedPtr<FSession> Session = Entry ? *Entry', 'const auto Factory = Session->ContentFactory', 'Impl->IsCurrent(Id, Session)', 'Session->Browser == Browser', 'Session->TabEpoch == PresentationEpoch', 'const uint64 Request = ++Session->DockRequest', 'Sessions.GenerateValueArray(Snapshot)', 'NextPresentationGeneration'):
            self.assertIn(marker,src)
        stop=src[src.index('    void Stop()'):src.index('FAuroraViewReply FAuroraViewReply::Success')]
        self.assertLess(stop.index('Sessions.Reset()'),stop.index('Session->Dispose'))
        native=(ROOT/'Source/AuroraViewEditor/Private/Tests/AuroraViewDockReentrancyTests.cpp').read_text()
        for marker in ('I < 1024','Module.Close(CloseId)','Module.Remove(ReplaceId)','Module.Close(ReopenId)','Interrupted factories leave no orphan live tabs'):
            self.assertIn(marker,native)
        self.assertIn('State->RetiredDirectTab.IsValid() && !State->RetiredDirectTab->GetParent().IsValid()',native)
        self.assertIn('DirectTab.IsValid() && DirectTab == State->RetiredDirectTab',native)
    def test_error_tab_close_callback_precedes_browser_open(self):
        src=(ROOT/'Source/AuroraViewRuntime/Private/AuroraViewRuntimeModule.cpp').read_text()
        spawn=src[src.index('FOnSpawnTab::CreateLambda'):src.index('bool FAuroraViewRuntimeModule::OpenDocked')]
        self.assertLess(spawn.index('Session->OwnDockTab(Tab)'),spawn.index('OpenPresentation'))
        self.assertIn('Current->TabEpoch == OwnedEpoch',src)
        native=(ROOT/'Source/AuroraViewEditor/Private/Tests/AuroraViewDockRecoveryTests.cpp').read_text()
        self.assertIn('RetainedClosedTab->RequestCloseTab()',native)
        self.assertIn('NewTab != State->RetainedClosedTab',native)
    def test_fixture_roles_map_and_mutation_revalidation(self):
        fixture=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewFixture.cpp').read_text()
        for marker in ('AuroraViewFixtureMap=', 'RoleCount != 1', 'Roles[Role].IsValid()', 'GetStaticMesh() != Cube', 'OutActors = Roles'):
            self.assertIn(marker,fixture)
        run=(ROOT/'scripts/run_native_acceptance.ps1').read_text()
        self.assertIn('$Project, $FixtureMap',run)
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeShowcase.cpp').read_text()
        for marker in ('TWeakObjectPtr<USceneComponent>', 'if (!Current()) return Interrupted()', 'TGuardValue<bool> MutationGuard', 'OutlinerHosts', 'IsInteractionCurrent(WidgetGeneration)', 'selectedActorCount', 'selectionTruncated'):
            self.assertIn(marker,src)
        self.assertNotIn('original value restored',src)
    def test_actor_drag_admission_is_complete_and_revalidated(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeShowcase.cpp').read_text()
        capture=src[src.index('FAuroraViewActorDragCapture FAuroraViewNativeShowcase::CaptureCompleteActorSelection'):src.index('AuroraView::ActorDragDecision FAuroraViewNativeShowcase::CheckActorDrag')]
        for marker in ('Selection->Num()', 'Selection->GetSelectedObject(Index)', 'Capture.Admission.Selection.push_back(Entry)'):
            self.assertIn(marker,capture)
        self.assertNotIn('GetSelectedActors() const',capture)
        self.assertNotIn('continue;',capture)
        self.assertNotIn('break;',capture)
        drag=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeDrag.cpp').read_text()
        actor=drag[drag.index('        if (bActors)'):drag.index('        const auto Assets')]
        self.assertNotIn('GetSelectedActors()',actor)
        self.assertLess(actor.index('PrepareActorDrag'),actor.index('FActorDragDropGraphEdOp::New'))
        self.assertLess(actor.index('FActorDragDropGraphEdOp::New'),actor.index('FinalizeActorDrag'))
        self.assertLess(actor.index('FinalizeActorDrag'),actor.index('BeginDragDrop(Operation)'))
        record=src[src.index('void FAuroraViewNativeShowcase::RecordActorDragDecision'):src.index('bool FAuroraViewNativeShowcase::PrepareActorDrag')]
        self.assertNotIn('RefreshScope();',record)
    def test_retired_outliners_never_rebuild(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewNativeShowcase.cpp').read_text()
        refresh=src[src.index('void FAuroraViewNativeShowcase::RefreshScope'):src.index('FString FAuroraViewNativeShowcase::IdFor')]
        self.assertIn('else ClearOutliners()',refresh)
        self.assertIn('if (!Module.GetGeneration(Id) || bStopped) ClearOutliners()',refresh)
        native=(ROOT/'Source/AuroraViewEditor/Private/Tests/AuroraViewNativeInteractionTests.cpp').read_text()
        for marker in ('RetainedClosed', 'RetainedRemoved', 'SetActorDragFeedbackForTesting', 'GetOutlinerBuildCountForTesting', 'GetChildAt(0) == SNullWidget::NullWidget'):
            self.assertIn(marker,native)
    def test_native_map_preparation_has_isolation_and_disk_save_guards(self):
        src=(ROOT/'Source/AuroraViewEditor/Private/AuroraViewPrepareCommandlet.cpp').read_text()
        for marker in ('AuroraViewNativeFixture', '/Game/AuroraViewAcceptance/', 'IsValidLongPackageName',
                       'FileExists(*Filename)', 'Refusing to overwrite', 'FEditorFileUtils::SaveMap',
                       'FileSize(*Filename) <= 0', 'fixture.json'):
            self.assertIn(marker,src)
        self.assertLess(src.index('FileExists(*Filename)'),src.index('GEditor->NewMap()'))
        self.assertLess(src.index('FileSize(*Filename) <= 0'),src.index('Receipt->SetBoolField(TEXT("saved"), true)'))
if __name__=='__main__':unittest.main()
