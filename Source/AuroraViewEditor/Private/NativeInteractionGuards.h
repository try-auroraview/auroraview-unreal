#pragma once

#include <cstddef>
#include <cstdint>
#include <unordered_set>
#include <vector>

namespace AuroraView
{
// Production admission policy, deliberately independent of Unreal so all edge
// cases can be executed without pretending that a host/Slate test took place.
constexpr std::size_t MaxActorDragSelection = 128;
struct ActorDragEntry
{
    std::uintptr_t Identity = 0, World = 0;
    bool IsActor = false, Valid = false, Destroying = false;
};
struct ActorDragSnapshot
{
    std::uint64_t Generation = 0, WidgetGeneration = 0;
    std::uintptr_t World = 0;
    bool Ready = false;
    std::vector<ActorDragEntry> Selection;
};
struct ActorDragDecision
{
    bool Allowed = false;
    const char* Reason = "unavailable";
    std::size_t Selected = 0, Eligible = 0, Invalid = 0, ForeignWorld = 0, Destroying = 0, NonActors = 0;
};
inline ActorDragDecision AdmitActorDrag(const ActorDragSnapshot& State)
{
    ActorDragDecision Result;
    Result.Selected = State.Selection.size();
    std::unordered_set<std::uintptr_t> Identities;
    bool Duplicate = false;
    for (const auto& Actor : State.Selection)
    {
        if (!Actor.Valid || !Actor.Identity) ++Result.Invalid;
        else if (!Actor.IsActor) ++Result.NonActors;
        else if (Actor.Destroying) ++Result.Destroying;
        else if (!Actor.World || Actor.World != State.World) ++Result.ForeignWorld;
        else ++Result.Eligible;
        if (!Identities.insert(Actor.Identity).second) Duplicate = true;
    }
    if (!State.Ready || !State.Generation || State.Generation != State.WidgetGeneration || !State.World)
        Result.Reason = "session_or_world_unavailable";
    else if (!Result.Selected) Result.Reason = "empty_selection";
    else if (Result.Selected > MaxActorDragSelection) Result.Reason = "selection_limit";
    else if (Result.NonActors) Result.Reason = "non_actor_selection";
    else if (Result.Invalid) Result.Reason = "invalid_actor";
    else if (Result.Destroying) Result.Reason = "destroying_actor";
    else if (Result.ForeignWorld) Result.Reason = "foreign_world_actor";
    else if (Duplicate) Result.Reason = "duplicate_actor";
    else { Result.Allowed = true; Result.Reason = "complete_selection"; }
    return Result;
}
inline ActorDragDecision RevalidateActorDrag(const ActorDragSnapshot& Saved, const ActorDragSnapshot& Current)
{
    auto Result = AdmitActorDrag(Current);
    if (!Result.Allowed) return Result;
    const auto Before = AdmitActorDrag(Saved);
    if (!Before.Allowed) return Before;
    Result.Allowed = false;
    if (Current.Generation != Saved.Generation || Current.WidgetGeneration != Saved.WidgetGeneration)
    { Result.Reason = "generation_changed"; return Result; }
    if (Current.World != Saved.World) { Result.Reason = "world_changed"; return Result; }
    if (Current.Selection.size() != Saved.Selection.size())
    { Result.Reason = "selection_changed"; return Result; }
    std::unordered_set<std::uintptr_t> Original;
    for (const auto& Actor : Saved.Selection) Original.insert(Actor.Identity);
    for (const auto& Actor : Current.Selection)
        if (!Original.count(Actor.Identity)) { Result.Reason = "selection_changed"; return Result; }
    Result.Allowed = true; Result.Reason = "complete_selection";
    return Result;
}
inline bool CanBuildNativeOutliner(std::uint64_t OwnedGeneration, std::uint64_t LiveGeneration,
    bool Stopped, bool HasIdleWorld)
{
    return !Stopped && HasIdleWorld && OwnedGeneration != 0 && OwnedGeneration == LiveGeneration;
}
}
