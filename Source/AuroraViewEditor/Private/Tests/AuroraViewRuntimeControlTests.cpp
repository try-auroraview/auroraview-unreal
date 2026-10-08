#include "AuroraViewRuntimeModule.h"
#include "Dom/JsonObject.h"
#include "GameFramework/GameUserSettings.h"
#include "Misc/AutomationTest.h"
#include "Misc/EngineVersion.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "Modules/ModuleManager.h"

#if WITH_DEV_AUTOMATION_TESTS
IMPLEMENT_SIMPLE_AUTOMATION_TEST(FAuroraViewRuntimeControlTest, "AuroraView.Runtime.ControlReflection",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)
bool FAuroraViewRuntimeControlTest::RunTest(const FString&)
{
    auto& Runtime = FModuleManager::LoadModuleChecked<FAuroraViewRuntimeModule>(TEXT("AuroraViewRuntime"));
    auto Invoke = [&Runtime](const FString& Name, const TSharedPtr<FJsonObject>& Params)
    {
        FAuroraViewReply Reply;
        bool Completed = false;
        Runtime.CallTool(Name, Params.IsValid() ? MakeShared<FJsonValueObject>(Params) : TSharedPtr<FJsonValue>(),
            [&Reply, &Completed](FAuroraViewReply Value) { Reply = MoveTemp(Value); Completed = true; });
        check(Completed); // These native tools must complete on this GameThread.
        return Reply;
    };
    auto Info = Invoke(TEXT("unreal.engine.info"), nullptr);
    TestTrue(TEXT("Actual native host reports engine identity"), Info.bOk && Info.Result.IsValid());
    if (!Info.bOk || !Info.Result.IsValid()) return false;
    TestTrue(TEXT("Isolated acceptance explicitly enables native control"), Info.Result->AsObject()->GetBoolField(TEXT("native_control")));
    auto ViewArgs = MakeShared<FJsonObject>();
    ViewArgs->SetStringField(TEXT("id"), TEXT("ControlInvalidPresentation"));
    ViewArgs->SetStringField(TEXT("html"), TEXT("<p>Invalid presentation never opens CEF</p>"));
    ViewArgs->SetStringField(TEXT("presentation"), TEXT("unknown"));
    auto InvalidPresentation = Invoke(TEXT("auroraview.view.open"), ViewArgs);
    TestTrue(TEXT("Unknown presentation fails before opening a browser"), !InvalidPresentation.bOk && InvalidPresentation.ErrorCode == TEXT("INVALID_PARAMS"));
    ViewArgs->SetNumberField(TEXT("presentation"), 7);
    InvalidPresentation = Invoke(TEXT("auroraview.view.open"), ViewArgs);
    TestTrue(TEXT("Presentation requires a string discriminator"), !InvalidPresentation.bOk && InvalidPresentation.ErrorCode == TEXT("INVALID_PARAMS"));
    auto MissingView = Invoke(TEXT("auroraview.view.describe"), ViewArgs);
    TestTrue(TEXT("Rejected presentation creates no native view"), MissingView.bOk && !MissingView.Result->AsObject()->GetBoolField(TEXT("exists")));
    if (FParse::Param(FCommandLine::Get(), TEXT("AuroraViewExpectEditorPython")))
        TestTrue(TEXT("Fixture-enabled installed Python capability is present"), Info.Result->AsObject()->GetBoolField(TEXT("editor_python")));
    auto Args = MakeShared<FJsonObject>();
    Args->SetStringField(TEXT("object"), TEXT("/Script/Engine.Default__KismetSystemLibrary"));
    Args->SetStringField(TEXT("function"), TEXT("GetEngineVersion"));
    Args->SetObjectField(TEXT("args"), MakeShared<FJsonObject>());
    auto Version = Invoke(TEXT("unreal.object.call"), Args);
    TestTrue(TEXT("Real ProcessEvent invokes a native UFunction"), Version.bOk);
    if (Version.bOk) TestTrue(TEXT("Reflected return value is the actual engine"),
        Version.Result->AsObject()->GetStringField(TEXT("return_value")).StartsWith(
            FString::Printf(TEXT("%d.%d"), FEngineVersion::Current().GetMajor(), FEngineVersion::Current().GetMinor())));
    auto BadArgs = MakeShared<FJsonObject>(); BadArgs->SetBoolField(TEXT("unknown"), true); Args->SetObjectField(TEXT("args"), BadArgs);
    auto Rejected = Invoke(TEXT("unreal.object.call"), Args);
    TestTrue(TEXT("Unknown arguments fail before ProcessEvent"), !Rejected.bOk && Rejected.ErrorCode == TEXT("INVALID_PARAMS"));
    auto* Settings = GetMutableDefault<UGameUserSettings>();
    auto Property = MakeShared<FJsonObject>(); Property->SetStringField(TEXT("object"), Settings->GetPathName()); Property->SetStringField(TEXT("property"), TEXT("bUseVSync"));
    auto Original = Invoke(TEXT("unreal.object.get"), Property);
    TestTrue(TEXT("Native property read succeeds"), Original.bOk && Original.Result.IsValid());
    if (Original.bOk && Original.Result.IsValid())
    {
        Property->SetBoolField(TEXT("value"), !Original.Result->AsBool());
        auto Changed = Invoke(TEXT("unreal.object.set"), Property);
        TestTrue(TEXT("Native property write and readback agree"), Changed.bOk && Changed.Result->AsBool() != Original.Result->AsBool());
        Property->SetField(TEXT("value"), Original.Result);
        auto Restored = Invoke(TEXT("unreal.object.set"), Property);
        TestTrue(TEXT("Fixture property is restored"), Restored.bOk && Restored.Result->AsBool() == Original.Result->AsBool());
    }
    TestTrue(TEXT("Project tools register independently of Editor APIs"), Runtime.RegisterTool(TEXT("native.acceptance.echo"),
        [&Runtime](const TSharedPtr<FJsonValue>& Value) { Runtime.UnregisterTool(TEXT("native.acceptance.echo")); return FAuroraViewReply::Success(Value); }));
    auto EchoArgs = MakeShared<FJsonObject>(); EchoArgs->SetNumberField(TEXT("value"), 42);
    auto Echo = Invoke(TEXT("native.acceptance.echo"), EchoArgs);
    TestTrue(TEXT("Reentrant tool removal preserves the current result"), Echo.bOk && Echo.Result->AsObject()->GetNumberField(TEXT("value")) == 42);
    auto Removed = Invoke(TEXT("native.acceptance.echo"), EchoArgs);
    TestTrue(TEXT("Removed tools cannot execute"), !Removed.bOk && Removed.ErrorCode == TEXT("METHOD_NOT_FOUND"));
    if (Info.Result->AsObject()->GetBoolField(TEXT("editor_python")))
    {
        auto Python = MakeShared<FJsonObject>(); Python->SetStringField(TEXT("code"), TEXT("auroraview_acceptance_value = 6 * 7\nassert auroraview_acceptance_value == 42"));
        auto Executed = Invoke(TEXT("unreal.python.execute"), Python);
        TestTrue(TEXT("Optional installed Editor Python executes real code"), Executed.bOk && Executed.Result->AsObject()->GetBoolField(TEXT("return_value")));
    }
    return true;
}
#endif
