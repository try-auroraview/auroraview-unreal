#include "AuroraViewPrepareCommandlet.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/World.h"
#include "FileHelpers.h"
#include "HAL/FileManager.h"
#include "Misc/App.h"
#include "Misc/FeedbackContext.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Parse.h"
#include "Misc/Paths.h"
#include "Runtime/Launch/Resources/Version.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/Package.h"
#include "UObject/UObjectGlobals.h"

UAuroraViewPrepareCommandlet::UAuroraViewPrepareCommandlet(const FObjectInitializer& ObjectInitializer)
    : Super(ObjectInitializer)
{
    IsEditor = true;
    IsClient = false;
    IsServer = false;
    LogToConsole = true;
    ShowErrorCount = true;
}

int32 UAuroraViewPrepareCommandlet::Main(const FString& Params)
{
    FString Map;
    if (FString(FApp::GetProjectName()) != TEXT("AuroraViewNativeFixture") || !GEditor
        || !FParse::Value(*Params, TEXT("Map="), Map)
        || !Map.StartsWith(TEXT("/Game/AuroraViewAcceptance/")) || Map.Contains(TEXT(".."))
        || !FPackageName::IsValidLongPackageName(Map))
    {
        UE_LOG(LogTemp, Error, TEXT("AuroraViewPrepare requires the disposable AuroraViewNativeFixture project and -Map=/Game/AuroraViewAcceptance/<Map>"));
        return 1;
    }
    const FString Filename = FPackageName::LongPackageNameToFilename(Map, FPackageName::GetMapPackageExtension());
    if (IFileManager::Get().FileExists(*Filename))
    {
        UE_LOG(LogTemp, Error, TEXT("Refusing to overwrite an existing fixture map"));
        return 2;
    }
    IFileManager::Get().MakeDirectory(*FPaths::GetPath(Filename), true);
    bool bSaved = false;
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION == 18
    // UE4.18's map helper requires GUI editor state, even in a commandlet.
    // Build an independent world and save its package without replacing GWorld.
    UPackage* Package = CreatePackage(nullptr, *Map);
    UWorld* World = Package ? UWorld::CreateWorld(EWorldType::Editor, false,
        FName(*FPackageName::GetLongPackageAssetName(Map)), Package, true) : nullptr;
    if (World)
    {
        World->SetFlags(RF_Public | RF_Standalone);
        bSaved = UPackage::SavePackage(Package, World, RF_NoFlags, *Filename,
            GWarn, nullptr, false, false, SAVE_None, nullptr, FDateTime::MinValue(), false);
        World->DestroyWorld(false);
    }
#else
    UWorld* World = GEditor->NewMap();
    bSaved = World && UEditorLoadingAndSavingUtils::SaveMap(World, Map);
#endif
    if (!bSaved || IFileManager::Get().FileSize(*Filename) <= 0)
    {
        UE_LOG(LogTemp, Error, TEXT("Could not create and save the native acceptance map"));
        return 3;
    }
    FString Project = FPaths::ConvertRelativePathToFull(FPaths::ProjectDir());
    FPaths::NormalizeDirectoryName(Project);
    const FString EvidenceDirectory = FPaths::Combine(Project, TEXT("evidence"));
    IFileManager::Get().MakeDirectory(*EvidenceDirectory, true);
    const auto Receipt = MakeShared<FJsonObject>();
    Receipt->SetBoolField(TEXT("saved"), true);
    Receipt->SetStringField(TEXT("map"), Map);
    Receipt->SetStringField(TEXT("project"), Project);
    FString Json;
    FJsonSerializer::Serialize(Receipt, TJsonWriterFactory<>::Create(&Json));
    if (!FFileHelper::SaveStringToFile(Json, *FPaths::Combine(EvidenceDirectory, TEXT("fixture.json"))))
    {
        UE_LOG(LogTemp, Error, TEXT("Could not write the fixture preparation receipt"));
        return 4;
    }
    UE_LOG(LogTemp, Display, TEXT("AuroraView native fixture map saved: %s"), *Map);
    return 0;
}
