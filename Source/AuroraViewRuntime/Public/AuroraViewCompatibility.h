#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "Framework/Docking/TabManager.h"
#include "Misc/CoreDelegates.h"
#include "Runtime/Launch/Resources/Version.h"

// The build matrix is an explicit admission contract, not a claim that every
// intermediate engine or a different platform has been validated.
#if !PLATFORM_WINDOWS || !((ENGINE_MAJOR_VERSION == 4 && (ENGINE_MINOR_VERSION == 18 || ENGINE_MINOR_VERSION == 26)) || (ENGINE_MAJOR_VERSION == 5 && (ENGINE_MINOR_VERSION == 5 || ENGINE_MINOR_VERSION == 7 || ENGINE_MINOR_VERSION == 8)))
#error AuroraView supports the Win64 UE 4.18, 4.26, 5.5, 5.7 and 5.8 build matrix only.
#endif

namespace AuroraViewCompatibility
{
inline FString UInt64String(uint64 Value)
{
    return FString::Printf(TEXT("%llu"), static_cast<unsigned long long>(Value));
}

#if ENGINE_MAJOR_VERSION >= 5
using FTicker = FTSTicker;
using FTickerHandle = FTSTicker::FDelegateHandle;
inline FSimpleMulticastDelegate& PreExit() { return FCoreDelegates::OnEnginePreExit; }
#else
using FTicker = ::FTicker;
using FTickerHandle = FDelegateHandle;
inline FSimpleMulticastDelegate& PreExit() { return FCoreDelegates::OnPreExit; }
#endif

inline TSharedPtr<SDockTab> TryInvokeTab(const TSharedRef<FTabManager>& Manager, FName Id)
{
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION < 26
    // InvokeTab asserts if the spawner is missing on this engine.
    if (!Manager->CanSpawnTab(Id)) return nullptr;
    return Manager->InvokeTab(Id);
#else
    return Manager->TryInvokeTab(Id);
#endif
}

inline bool HasTabSpawner(const TSharedRef<FTabManager>& Manager, FName Id)
{
#if ENGINE_MAJOR_VERSION == 4 && ENGINE_MINOR_VERSION < 23
    return Manager->CanSpawnTab(Id);
#else
    return Manager->HasTabSpawner(Id);
#endif
}
}
