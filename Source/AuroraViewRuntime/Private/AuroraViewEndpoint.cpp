#include "AuroraViewEndpoint.h"

void UAuroraViewEndpoint::Initialize(std::shared_ptr<AuroraView::SessionMailbox> InMailbox,
                                  uint64 InGeneration, FString InToken)
{
    check(IsInGameThread());
    check(!Mailbox);
    Mailbox = MoveTemp(InMailbox);
    Generation = InGeneration;
    Token = MoveTemp(InToken);
}

bool UAuroraViewEndpoint::PostMessage(const FString& Payload, const FString& SessionToken)
{
    // No Unreal/editor operation here. The module drains on its GameThread ticker.
    if (!Mailbox || Token != SessionToken || Payload.Len() > static_cast<int32>(AuroraView::SessionMailbox::MaxBytes))
        return false;
    FTCHARToUTF8 Utf8(*Payload);
    return Mailbox->Push(Generation, std::string(Utf8.Get(), Utf8.Length()));
}

bool UAuroraViewEndpoint::MarkReady(const FString& SessionToken)
{
    return Mailbox && Token == SessionToken
        && Mailbox->PushControl(Generation, AuroraView::SessionMailbox::Kind::Ready);
}
