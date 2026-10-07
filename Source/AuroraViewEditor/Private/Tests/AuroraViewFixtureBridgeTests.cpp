#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewFixture.h"
#include "AuroraViewNativeShowcase.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "GameFramework/Actor.h"
#include "HAL/PlatformTime.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"

namespace
{
const FName FixtureView(TEXT("FixtureBridgeAutomation"));
struct FFixtureBridgeState
{
    TSharedPtr<FAuroraViewNativeShowcase> Host;
    TWeakObjectPtr<AActor> Actor;
    FTransform Before;
    bool bReported = false, bBrowserVerified = false, bNativeVerified = false;
    double Deadline = 0;
};
class FWaitForFixtureBridge final : public IAutomationLatentCommand
{
public:
    FWaitForFixtureBridge(FAutomationTestBase* InTest, TSharedRef<FFixtureBridgeState> InState) : Test(InTest), State(InState) {}
    bool Update() override
    {
        if (!State->bReported && FPlatformTime::Seconds() < State->Deadline) return false;
        Test->TestTrue(TEXT("Real CEF verified typed transform results and host event"), State->bBrowserVerified);
        Test->TestTrue(TEXT("Native UObject matched browser-requested edit"), State->bNativeVerified);
        Test->TestTrue(TEXT("Real CEF restored original native actor transform"), State->Actor.IsValid()
            && State->Actor->GetActorTransform().Equals(State->Before, 0.0001));
        // On failure, the typed restore is conditional and never overwrites an
        // unrelated intervening edit. Keep the failure in the Automation report.
        State->Host->Dispatch(TEXT("showcase.restoreTransform"), MakeShared<FJsonValueNull>());
        State->Host->Stop();
        FModuleManager::GetModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor")).Remove(FixtureView);
        return true;
    }
private:
    FAutomationTestBase* Test;
    TSharedRef<FFixtureBridgeState> State;
};
}
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewFixtureBridge, "AuroraView.Showcase.FixtureBridgeRoundTrip",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewFixtureBridge::RunTest(const FString&)
{
    FString Error; TArray<TWeakObjectPtr<AActor>> Actors;
    if (!AuroraViewFixture::Create(Actors, Error)) { AddError(Error); return false; }
    if (Actors.Num() != 3 || !Actors[0].IsValid()) { AddError(TEXT("Fixture unavailable")); return false; }
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    Module.Remove(FixtureView);
    const auto State = MakeShared<FFixtureBridgeState>();
    State->Actor = Actors[0]; State->Before = Actors[0]->GetActorTransform();
    State->Deadline = FPlatformTime::Seconds() + 25;
    State->Host = MakeShared<FAuroraViewNativeShowcase>(Module, FixtureView);
    GEditor->SelectNone(false, true, false); GEditor->SelectActor(Actors[0].Get(), true, true);
    const FString Html = TEXT("<h1>Actual CEF → typed native fixture → CEF</h1><script>")
        TEXT("addEventListener('auroraviewready',async()=>{try{let pushed=false;")
        TEXT("auroraview.on('showcase.state',s=>{pushed=s.gameThread===true;});")
        TEXT("const s=await auroraview.call('showcase.subscribe',{});const a=s.actors.find(x=>x.selected);")
        TEXT("const next=JSON.parse(JSON.stringify(a.transform));next.location[0]+=50;")
        TEXT("const edited=await auroraview.call('showcase.setTransform',{actorId:a.id,expected:a.transform,transform:next});")
        TEXT("const native=await auroraview.call('test.checkNative',{});")
        TEXT("const restored=await auroraview.call('showcase.restoreTransform',{});")
        TEXT("await new Promise(r=>setTimeout(r,600));")
        TEXT("await auroraview.call('test.report',{ok:edited.transformMatched===true&&native===true&&pushed&&")
        TEXT("restored.actors.find(x=>x.id===a.id).transform.location[0]===a.transform.location[0]});")
        TEXT("}catch(e){auroraview.call('test.report',{ok:false,error:e.message});}});</script>");
    if (!State->Host->Start(Html, Error)) { AddError(Error); State->Host->Stop(); Module.Remove(FixtureView); return false; }
    Module.BindCall(FixtureView, TEXT("test.checkNative"), [State](const TSharedPtr<FJsonValue>&)
    {
        State->bNativeVerified = IsInGameThread() && State->Actor.IsValid()
            && State->Actor->GetActorLocation().Equals(State->Before.GetLocation() + FVector(50, 0, 0), 0.0001);
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(State->bNativeVerified));
    });
    Module.BindCall(FixtureView, TEXT("test.report"), [State](const TSharedPtr<FJsonValue>& Params)
    {
        State->bReported = true;
        if (Params.IsValid() && Params->Type == EJson::Object)
            Params->AsObject()->TryGetBoolField(TEXT("ok"), State->bBrowserVerified);
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true));
    });
    if (!Module.OpenDocked(FixtureView, Error))
    { AddError(Error); State->Host->Stop(); Module.Remove(FixtureView); return false; }
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForFixtureBridge(this, State));
    return true;
}
#endif
