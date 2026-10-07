#include "AuroraViewNativeControl.h"
#include "Dom/JsonObject.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "HAL/PlatformProcess.h"
#include "JsonObjectConverter.h"
#include "Misc/EngineVersion.h"
#include "Modules/ModuleManager.h"
#include "Runtime/Launch/Resources/Version.h"
#include "UObject/StructOnScope.h"
#include "UObject/StrongObjectPtr.h"
#include "UObject/UnrealType.h"

namespace
{
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION < 25
using FControlProperty = UProperty;
#else
using FControlProperty = FProperty;
#endif

FAuroraViewReply ControlError(const FString& Code, const FString& Message)
{
    return FAuroraViewReply::Failure(TEXT("UnrealControlError"), Message, Code);
}

TSharedPtr<FJsonObject> ObjectParams(const TSharedPtr<FJsonValue>& Params)
{
    return Params.IsValid() && Params->Type == EJson::Object ? Params->AsObject() : nullptr;
}

bool StringField(const TSharedPtr<FJsonObject>& Params, const TCHAR* Name, FString& Value)
{
    if (!Params.IsValid()) return false;
    const auto* Field = Params->Values.Find(Name);
    if (!Field || !Field->IsValid() || (*Field)->Type != EJson::String) return false;
    Value = (*Field)->AsString();
    return !Value.IsEmpty() && Value.Len() <= 4096;
}

UObject* ResolveObject(const TSharedPtr<FJsonObject>& Params)
{
    FString Path;
    if (!StringField(Params, TEXT("object"), Path) || !Path.StartsWith(TEXT("/"))) return nullptr;
    UObject* Object = StaticFindObject(UObject::StaticClass(), nullptr, *Path);
    return IsValid(Object) ? Object : nullptr;
}

FControlProperty* FindProperty(UStruct* Struct, const FString& Name)
{
    for (TFieldIterator<FControlProperty> It(Struct); It; ++It)
        if (It->GetName() == Name) return *It;
    return nullptr;
}

TSharedRef<FJsonObject> PropertyDescription(FControlProperty* Property)
{
    auto Result = MakeShared<FJsonObject>();
    Result->SetStringField(TEXT("name"), Property->GetName());
    Result->SetStringField(TEXT("type"), Property->GetCPPType());
    Result->SetBoolField(TEXT("out"), Property->HasAnyPropertyFlags(CPF_OutParm | CPF_ReturnParm));
    Result->SetBoolField(TEXT("return"), Property->HasAnyPropertyFlags(CPF_ReturnParm));
    return Result;
}

FAuroraViewReply Call(UObject* Object, UFunction* Function, const TSharedPtr<FJsonObject>& Args)
{
    if (!Object || !Function) return ControlError(TEXT("NOT_FOUND"), TEXT("Loaded object or reflected function not found"));
    if (Function->ParmsSize > 65536 || Function->NumParms > 128)
        return ControlError(TEXT("LIMIT"), TEXT("Function parameter storage exceeds the synchronous tool limit"));
    TStrongObjectPtr<UObject> ObjectOwner(Object);
    TStrongObjectPtr<UFunction> FunctionOwner(Function);
    FStructOnScope Storage(Function);
    auto* Memory = Storage.GetStructMemory();
    TSet<FString> InputNames;
    for (TFieldIterator<FControlProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        FControlProperty* Property = *It;
        if (Property->GetCPPType().Contains(TEXT("FLatentActionInfo")))
            return ControlError(TEXT("ASYNC_REQUIRED"), TEXT("Latent UFunctions require a project tool with explicit completion ownership"));
        if (Property->HasAnyPropertyFlags(CPF_ReturnParm)) continue;
        const bool bPureOut = Property->HasAnyPropertyFlags(CPF_OutParm)
            && !Property->HasAnyPropertyFlags(CPF_ReferenceParm | CPF_ConstParm);
        if (bPureOut) continue;
        InputNames.Add(Property->GetName());
        const auto Value = Args.IsValid() ? Args->TryGetField(Property->GetName()) : TSharedPtr<FJsonValue>();
        if (!Value.IsValid()) return ControlError(TEXT("INVALID_PARAMS"), TEXT("Missing argument: ") + Property->GetName());
        if (!FJsonObjectConverter::JsonValueToUProperty(Value, Property,
            Property->ContainerPtrToValuePtr<void>(Memory), 0, 0))
            return ControlError(TEXT("INVALID_PARAMS"), TEXT("Cannot convert argument: ") + Property->GetName());
    }
    if (Args.IsValid()) for (const auto& Pair : Args->Values)
    {
        // UE5.8 stores JSON keys as FSharedString. Preserve the full length,
        // including embedded nulls, when checking the declared argument names.
        const FString Key(Pair.Key.Len(), *Pair.Key);
        if (!InputNames.Contains(Key)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("Unknown argument: ") + Key);
    }
    Object->ProcessEvent(Function, Memory);
    auto Result = MakeShared<FJsonObject>();
    auto Outputs = MakeShared<FJsonObject>();
    for (TFieldIterator<FControlProperty> It(Function); It && It->HasAnyPropertyFlags(CPF_Parm); ++It)
    {
        if (!It->HasAnyPropertyFlags(CPF_OutParm | CPF_ReturnParm)) continue;
        auto Value = FJsonObjectConverter::UPropertyToJsonValue(*It, It->ContainerPtrToValuePtr<void>(Memory), 0, 0);
        if (!Value.IsValid()) return ControlError(TEXT("SERIALIZATION"), TEXT("Cannot serialize output: ") + It->GetName());
        if (It->HasAnyPropertyFlags(CPF_ReturnParm)) Result->SetField(TEXT("return_value"), Value);
        else Outputs->SetField(It->GetName(), Value);
    }
    Result->SetObjectField(TEXT("out"), Outputs);
    return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
}

UWorld* ResolveWorld(const TSharedPtr<FJsonObject>& Params)
{
    if (!GEngine) return nullptr;
    FString Path;
    StringField(Params, TEXT("world"), Path);
    UWorld* Result = nullptr;
    for (const auto& Context : GEngine->GetWorldContexts())
    {
        UWorld* World = Context.World();
        if (!IsValid(World)) continue;
        if (!Path.IsEmpty()) { if (World->GetPathName() == Path) return World; continue; }
        if (World->WorldType != EWorldType::Game && World->WorldType != EWorldType::PIE && World->WorldType != EWorldType::Editor) continue;
        if (Result) return nullptr; // A caller must select a world when more than one exists.
        Result = World;
    }
    return Result;
}
}

bool AuroraViewNativeControl::HasEditorPython()
{
#if WITH_EDITOR
    return StaticFindObject(UClass::StaticClass(), nullptr, TEXT("/Script/PythonScriptPlugin.PythonScriptLibrary")) != nullptr;
#else
    return false;
#endif
}

FAuroraViewReply AuroraViewNativeControl::Execute(const FString& Method, const TSharedPtr<FJsonValue>& Params, bool bAllowControl)
{
    check(IsInGameThread());
    const auto Args = ObjectParams(Params);
    if (Method == TEXT("unreal.engine.info"))
    {
        auto Result = MakeShared<FJsonObject>();
        Result->SetStringField(TEXT("engine_version"), FEngineVersion::Current().ToString());
        Result->SetNumberField(TEXT("pid"), FPlatformProcess::GetCurrentProcessId());
        Result->SetStringField(TEXT("context"), GIsEditor ? TEXT("editor") : TEXT("game"));
        Result->SetBoolField(TEXT("engine_ready"), GEngine != nullptr);
        Result->SetBoolField(TEXT("native_control"), bAllowControl);
        Result->SetBoolField(TEXT("editor_python"), HasEditorPython());
        return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
    }
    if (!bAllowControl) return ControlError(TEXT("CONTROL_DISABLED"), TEXT("Start the host with -AuroraViewAllowControl to enable native Unreal tools"));
    if (Method == TEXT("unreal.world.list"))
    {
        TArray<TSharedPtr<FJsonValue>> Worlds;
        if (GEngine) for (const auto& Context : GEngine->GetWorldContexts())
        {
            auto* World = Context.World();
            if (!IsValid(World)) continue;
            auto Entry = MakeShared<FJsonObject>();
            Entry->SetStringField(TEXT("object"), World->GetPathName());
            Entry->SetStringField(TEXT("name"), World->GetName());
            Entry->SetNumberField(TEXT("world_type"), static_cast<int32>(World->WorldType));
            Worlds.Add(MakeShared<FJsonValueObject>(Entry));
        }
        return FAuroraViewReply::Success(MakeShared<FJsonValueArray>(Worlds));
    }
    if (Method == TEXT("unreal.actor.list"))
    {
        UWorld* World = ResolveWorld(Args);
        if (!World) return ControlError(TEXT("WORLD_REQUIRED"), TEXT("Select one live world by its object path"));
        TArray<TSharedPtr<FJsonValue>> Actors;
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            if (!IsValid(*It)) continue;
            if (Actors.Num() >= 4096) return ControlError(TEXT("LIMIT"), TEXT("Actor result exceeds 4096 entries; register a project query tool"));
            auto Entry = MakeShared<FJsonObject>();
            Entry->SetStringField(TEXT("object"), It->GetPathName());
            Entry->SetStringField(TEXT("name"), It->GetName());
            Entry->SetStringField(TEXT("class"), It->GetClass()->GetPathName());
            Actors.Add(MakeShared<FJsonValueObject>(Entry));
        }
        return FAuroraViewReply::Success(MakeShared<FJsonValueArray>(Actors));
    }
    if (Method == TEXT("unreal.console.execute"))
    {
        FString Command;
        UWorld* World = ResolveWorld(Args);
        if (!World || !StringField(Args, TEXT("command"), Command)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("A world and command are required"));
        const bool bHandled = GEngine->Exec(World, *Command);
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(bHandled));
    }
    if (Method == TEXT("unreal.python.execute"))
    {
#if WITH_EDITOR
        FString Code;
        if (!HasEditorPython()) return ControlError(TEXT("CAPABILITY_UNAVAILABLE"), TEXT("Enable the installed engine's PythonScriptPlugin; external Python tools work independently"));
        if (!StringField(Args, TEXT("code"), Code)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("A nonempty Python code string is required"));
        auto* Class = Cast<UClass>(StaticFindObject(UClass::StaticClass(), nullptr, TEXT("/Script/PythonScriptPlugin.PythonScriptLibrary")));
        auto PythonArgs = MakeShared<FJsonObject>();
        PythonArgs->SetStringField(TEXT("PythonCommand"), Code);
        return Call(Class ? Class->GetDefaultObject() : nullptr, Class ? Class->FindFunctionByName(TEXT("ExecutePythonCommand")) : nullptr, PythonArgs);
#else
        return ControlError(TEXT("CAPABILITY_UNAVAILABLE"), TEXT("Packaged games use external Python and native/project tools; Unreal's Python plugin is Editor-only"));
#endif
    }
    UObject* Object = ResolveObject(Args);
    if (!Object) return ControlError(TEXT("NOT_FOUND"), TEXT("Specify the full path of an already loaded Unreal object"));
    if (Method == TEXT("unreal.object.describe"))
    {
        auto Result = MakeShared<FJsonObject>();
        Result->SetStringField(TEXT("object"), Object->GetPathName());
        Result->SetStringField(TEXT("class"), Object->GetClass()->GetPathName());
        TArray<TSharedPtr<FJsonValue>> Properties, Functions;
        for (TFieldIterator<FControlProperty> It(Object->GetClass()); It; ++It)
            Properties.Add(MakeShared<FJsonValueObject>(PropertyDescription(*It)));
        for (TFieldIterator<UFunction> It(Object->GetClass()); It; ++It)
        {
            auto Function = MakeShared<FJsonObject>();
            Function->SetStringField(TEXT("name"), It->GetName());
            TArray<TSharedPtr<FJsonValue>> Parameters;
            for (TFieldIterator<FControlProperty> P(*It); P && P->HasAnyPropertyFlags(CPF_Parm); ++P)
                Parameters.Add(MakeShared<FJsonValueObject>(PropertyDescription(*P)));
            Function->SetArrayField(TEXT("parameters"), Parameters);
            Functions.Add(MakeShared<FJsonValueObject>(Function));
        }
        Result->SetArrayField(TEXT("properties"), Properties);
        Result->SetArrayField(TEXT("functions"), Functions);
        return FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Result));
    }
    if (Method == TEXT("unreal.object.call"))
    {
        FString Name;
        if (!StringField(Args, TEXT("function"), Name)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("A function name is required"));
        const auto* Values = Args->Values.Find(TEXT("args"));
        if (Values && (!Values->IsValid() || (*Values)->Type != EJson::Object)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("args must be an object"));
        return Call(Object, Object->FindFunction(FName(*Name)), Values ? (*Values)->AsObject() : MakeShared<FJsonObject>());
    }
    FString Name;
    if (!StringField(Args, TEXT("property"), Name)) return ControlError(TEXT("INVALID_PARAMS"), TEXT("A property name is required"));
    auto* Property = FindProperty(Object->GetClass(), Name);
    if (!Property) return ControlError(TEXT("NOT_FOUND"), TEXT("Reflected property not found"));
    void* Value = Property->ContainerPtrToValuePtr<void>(Object);
    if (Method == TEXT("unreal.object.get"))
    {
        auto Json = FJsonObjectConverter::UPropertyToJsonValue(Property, Value, 0, 0);
        return Json.IsValid() ? FAuroraViewReply::Success(Json) : ControlError(TEXT("SERIALIZATION"), TEXT("Cannot serialize the reflected property"));
    }
    if (Method == TEXT("unreal.object.set"))
    {
        const auto* NewValue = Args->Values.Find(TEXT("value"));
        if (!NewValue) return ControlError(TEXT("INVALID_PARAMS"), TEXT("value is required"));
        TStrongObjectPtr<UObject> ObjectOwner(Object);
        TStrongObjectPtr<UClass> ClassOwner(Object->GetClass());
        FDefaultConstructedPropertyElement Temporary(Property);
        void* Converted = Temporary.GetObjAddress();
        if (!FJsonObjectConverter::JsonValueToUProperty(*NewValue, Property, Converted, 0, 0))
            return ControlError(TEXT("INVALID_PARAMS"), TEXT("Cannot convert property value"));
#if WITH_EDITOR
        Object->Modify();
        Object->PreEditChange(Property);
#endif
        if (!IsValid(Object) || Object->GetClass() != ClassOwner.Get() || FindProperty(Object->GetClass(), Name) != Property)
            return ControlError(TEXT("OBJECT_CHANGED"), TEXT("Object or property changed during the edit notification"));
        Value = Property->ContainerPtrToValuePtr<void>(Object);
        Property->CopyCompleteValue(Value, Converted);
#if WITH_EDITOR
        FPropertyChangedEvent Change(Property, EPropertyChangeType::ValueSet);
        Object->PostEditChangeProperty(Change);
#endif
        if (!IsValid(Object) || Object->GetClass() != ClassOwner.Get() || FindProperty(Object->GetClass(), Name) != Property)
            return ControlError(TEXT("OBJECT_CHANGED"), TEXT("Object or property changed after the edit notification"));
        Value = Property->ContainerPtrToValuePtr<void>(Object);
        auto Json = FJsonObjectConverter::UPropertyToJsonValue(Property, Value, 0, 0);
        return Json.IsValid() ? FAuroraViewReply::Success(Json) : ControlError(TEXT("SERIALIZATION"), TEXT("Cannot serialize the edited property"));
    }
    return ControlError(TEXT("METHOD_NOT_FOUND"), TEXT("Unknown native Unreal method"));
}
