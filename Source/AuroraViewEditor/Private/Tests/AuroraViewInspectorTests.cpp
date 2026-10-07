#if WITH_DEV_AUTOMATION_TESTS
#include "AuroraViewNativeShowcase.h"
#include "Dom/JsonObject.h"
#include "Misc/AutomationTest.h"
#include "Modules/ModuleManager.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewTypedInspector, "AuroraView.Editor.TypedInspectorGuards",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewTypedInspector::RunTest(const FString&)
{
    auto& Module = FModuleManager::LoadModuleChecked<FAuroraViewEditorModule>(TEXT("AuroraViewEditor"));
    const auto Host = MakeShared<FAuroraViewNativeShowcase>(Module);
    // This test has no Start/ticker or selection/scene mutations.
    for (const auto& Value : { MakeShared<FJsonValueString>(TEXT("bad")), MakeShared<FJsonValueString>(TEXT("")) })
    {
        const auto Reply = Host->Dispatch(TEXT("showcase.setTransform"), Value);
        TestFalse(TEXT("Invalid parameter shape is rejected"), Reply.bOk);
        TestEqual(TEXT("Typed invalid input code"), Reply.ErrorCode, FString(TEXT("INVALID_PARAMS")));
    }
    const auto Params = MakeShared<FJsonObject>();
    Params->SetStringField(TEXT("actorId"), TEXT("/Game/SomeActor"));
    const auto Select = Host->Dispatch(TEXT("showcase.selectActor"), MakeShared<FJsonValueObject>(Params));
    TestFalse(TEXT("An arbitrary object path is never a selectable actor ID"), Select.bOk);
    TestEqual(TEXT("Absent opaque actor ID is stale"), Select.ErrorCode, FString(TEXT("STALE_ACTOR")));
    const auto State = Host->Snapshot();
    TestTrue(TEXT("Snapshot runs on the actual GameThread"), State->GetBoolField(TEXT("gameThread")));
    TestEqual(TEXT("Snapshot reflects current actual selected actors"),
        State->GetArrayField(TEXT("selectedActorIds")).Num(), Host->GetSelectedActors().Num());
    for (const auto& Id : State->GetArrayField(TEXT("selectedActorIds")))
        TestEqual(TEXT("Actor identity is an opaque GUID, not an object path"), Id->AsString().Len(), 32);
    TestFalse(TEXT("Restore without a native edit cannot mutate the world"),
        Host->Dispatch(TEXT("showcase.restoreTransform"), MakeShared<FJsonValueNull>()).bOk);
    return true;
}
#endif
