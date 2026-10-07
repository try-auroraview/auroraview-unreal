// Unreal-only smoke. Deliberately separate from cloud contract checks.
#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewEditorModule.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Misc/AutomationTest.h"
#include "HAL/PlatformTime.h"
#include "Modules/ModuleManager.h"

namespace
{
struct FBridgeSmokeState
{
    bool bEchoOnGameThread = false;
    bool bBrowserVerifiedResult = false;
    bool bInvalidTypeRejected = false;
    bool bMalformedHandlerRan = false;
    double Deadline = 0;
};

class FWaitForBridgeSmoke final : public IAutomationLatentCommand
{
public:
    FWaitForBridgeSmoke(FAutomationTestBase* InTest, TSharedRef<FBridgeSmokeState> InState)
        : Test(InTest), State(InState) {}
    virtual bool Update() override
    {
        if (!State->bBrowserVerifiedResult && FPlatformTime::Seconds() < State->Deadline) return false;
        Test->TestTrue(TEXT("Real browser received the echo result"), State->bBrowserVerifiedResult);
        Test->TestTrue(TEXT("Host handler executed on GameThread"), State->bEchoOnGameThread);
        Test->TestTrue(TEXT("Missing type receives INVALID_REQUEST"), State->bInvalidTypeRejected);
        Test->TestFalse(TEXT("Malformed request never invokes a host handler"), State->bMalformedHandlerRan);
        if (auto* Module = FModuleManager::GetModulePtr<FAuroraViewEditorModule>(TEXT("AuroraViewEditor")))
            Module->Remove(FName(TEXT("AuroraViewAutomation")));
        return true;
    }
private:
    FAutomationTestBase* Test;
    TSharedRef<FBridgeSmokeState> State;
};
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewBridgeSmoke, "AuroraView.Editor.BridgeRoundTrip",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FAuroraViewBridgeSmoke::RunTest(const FString& Parameters)
{
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    const FName Id(TEXT("AuroraViewAutomation"));
    const auto State = MakeShared<FBridgeSmokeState>();
    State->Deadline = FPlatformTime::Seconds() + 15.0;
    Module.BindCall(Id, TEXT("test.echo"), [State](const TSharedPtr<FJsonValue>& Params)
    {
        State->bEchoOnGameThread = IsInGameThread();
        return FAuroraViewReply::Success(Params);
    });
    Module.BindCall(Id, TEXT("test.report"), [State](const TSharedPtr<FJsonValue>& Params)
    {
        if (Params.IsValid() && Params->Type == EJson::Object)
        {
            const auto Object = Params->AsObject();
            Object->TryGetBoolField(TEXT("echo"), State->bBrowserVerifiedResult);
            Object->TryGetBoolField(TEXT("invalidType"), State->bInvalidTypeRejected);
        }
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true));
    });
    Module.BindCall(Id, TEXT("test.mustNotRun"), [State](const TSharedPtr<FJsonValue>&)
    {
        State->bMalformedHandlerRan = true;
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(false));
    });
    const FString Html = TEXT("<h1>AuroraView real Editor smoke</h1><script>")
        TEXT("addEventListener('auroraviewready',function(){var send=window.ipc.postMessage;")
        TEXT("window.ipc.postMessage=function(p){var m=JSON.parse(p);if(m.method==='test.mustNotRun')")
        TEXT("{delete m.type;if(m.params===1)m.type='';if(m.params===2)m.type=42;")
        TEXT("p=JSON.stringify(m);}send(p);};auroraview.call('test.echo',{n:42})")
        TEXT(".then(function(v){return Promise.all([0,1,2].map(function(kind){")
        TEXT("return auroraview.call('test.mustNotRun',kind).then(function(){return false;},")
        TEXT("function(e){return e.code==='INVALID_REQUEST';});})).then(function(rejected){")
        TEXT("return auroraview.call('test.report',{echo:v.n===42,")
        TEXT("invalidType:rejected.every(function(ok){return ok;})});});});});</script>");
    FString Error;
    if (!Module.Open(Id, Html, FText::FromString(TEXT("AuroraView automation")), Error))
    {
        AddError(Error);
        Module.Remove(Id);
        return false;
    }
    ADD_LATENT_AUTOMATION_COMMAND(FWaitForBridgeSmoke(this, State));
    return true;
}
#endif
