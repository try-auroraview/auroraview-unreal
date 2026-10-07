// Production pure-policy tests only: no claim of Unreal/Slate execution.
#include "NativeInteractionGuards.h"
#include <cassert>
#include <cstring>
#include <iostream>
#include <utility>

using namespace AuroraView;
ActorDragSnapshot Selection(std::size_t Count)
{
    ActorDragSnapshot State; State.Generation = State.WidgetGeneration = 7; State.World = 11; State.Ready = true;
    for (std::size_t I = 0; I < Count; ++I) State.Selection.push_back({ I + 100, 11, true, true, false });
    return State;
}
void Rejected(const ActorDragSnapshot& State, const char* Reason)
{
    const auto Result = AdmitActorDrag(State); assert(!Result.Allowed); assert(std::strcmp(Result.Reason, Reason) == 0);
}
int main()
{
    auto Full = Selection(128); const auto Accepted = AdmitActorDrag(Full);
    assert(Accepted.Allowed && Accepted.Selected == 128 && Accepted.Eligible == 128);
    Rejected(Selection(129), "selection_limit");
    auto State = Selection(2); State.Selection[1].World = 99;
    Rejected(State, "foreign_world_actor"); assert(AdmitActorDrag(State).ForeignWorld == 1);
    State = Selection(2); State.Selection[1].Valid = false; Rejected(State, "invalid_actor");
    State = Selection(2); State.Selection[1].Destroying = true; Rejected(State, "destroying_actor");
    State = Selection(2); State.Selection[1].IsActor = false; Rejected(State, "non_actor_selection");
    State = Selection(2); State.Selection[1] = State.Selection[0]; Rejected(State, "duplicate_actor");
    Rejected(Selection(0), "empty_selection");
    auto Saved = Selection(2); auto Current = Saved;
    Current.Selection[1].Valid = false; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Selection[1].Destroying = true; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Selection[1].Identity = 900; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Selection.pop_back(); assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Generation = Current.WidgetGeneration = 8; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.World = 12; for (auto& Actor : Current.Selection) Actor.World = 12;
    assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Ready = false; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; Current.Generation = 0; assert(!RevalidateActorDrag(Saved, Current).Allowed);
    Current = Saved; std::swap(Current.Selection[0], Current.Selection[1]); assert(RevalidateActorDrag(Saved, Current).Allowed);
    // The feedback/refresh seam must not skip final admission after callbacks.
    auto AcrossFeedback = [](ActorDragSnapshot Before, auto Feedback)
    { auto After = Before; Feedback(After); return RevalidateActorDrag(Before, After).Allowed; };
    assert(!AcrossFeedback(Saved, [](auto& S) { S.Selection = Selection(129).Selection; }));
    assert(!AcrossFeedback(Saved, [](auto& S) { S.Generation = S.WidgetGeneration = 44; }));
    assert(!AcrossFeedback(Saved, [](auto& S) { S.Selection[0].Identity = 333; }));
    assert(!AcrossFeedback(Saved, [](auto& S) { S.World = 21; }));
    assert(AcrossFeedback(Full, [](auto&) {}));
    assert(CanBuildNativeOutliner(7, 7, false, true));
    assert(!CanBuildNativeOutliner(0, 0, false, true));
    assert(!CanBuildNativeOutliner(7, 0, false, true));
    assert(!CanBuildNativeOutliner(7, 8, false, true));
    assert(!CanBuildNativeOutliner(7, 7, true, true));
    assert(!CanBuildNativeOutliner(7, 7, false, false));
    std::cout << "PASS: 28 production native-interaction policy cases (no Unreal execution)\n";
}
