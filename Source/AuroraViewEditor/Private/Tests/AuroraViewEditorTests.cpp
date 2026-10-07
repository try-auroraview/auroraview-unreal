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
    bool bReportReceived = false;
    FString MissingTypeCode;
    FString EmptyTypeCode;
    FString NumericTypeCode;
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
        if (!State->bReportReceived && FPlatformTime::Seconds() < State->Deadline) return false;
        Test->TestTrue(TEXT("Browser smoke report received"), State->bReportReceived);
        Test->TestTrue(TEXT("Real browser received the echo result"), State->bBrowserVerifiedResult);
        Test->TestTrue(TEXT("Host handler executed on GameThread"), State->bEchoOnGameThread);
        Test->AddInfo(FString::Printf(TEXT("Malformed type codes: missing=%s empty=%s numeric=%s"),
            *State->MissingTypeCode, *State->EmptyTypeCode, *State->NumericTypeCode));
        Test->TestEqual(TEXT("Missing type error code"), State->MissingTypeCode, FString(TEXT("INVALID_REQUEST")));
        Test->TestEqual(TEXT("Empty type error code"), State->EmptyTypeCode, FString(TEXT("INVALID_REQUEST")));
        Test->TestEqual(TEXT("Numeric type error code"), State->NumericTypeCode, FString(TEXT("INVALID_REQUEST")));
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
            Object->TryGetStringField(TEXT("missingTypeCode"), State->MissingTypeCode);
            Object->TryGetStringField(TEXT("emptyTypeCode"), State->EmptyTypeCode);
            Object->TryGetStringField(TEXT("numericTypeCode"), State->NumericTypeCode);
            State->bReportReceived = true;
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
        TEXT("return auroraview.call('test.mustNotRun',kind).then(function(){return 'UNEXPECTED_SUCCESS';},")
        TEXT("function(e){return typeof e.code==='string'?e.code:'MISSING_ERROR_CODE';});})).then(function(codes){")
        TEXT("return auroraview.call('test.report',{echo:v.n===42,")
        TEXT("missingTypeCode:codes[0],emptyTypeCode:codes[1],numericTypeCode:codes[2]});});});});</script>");
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
