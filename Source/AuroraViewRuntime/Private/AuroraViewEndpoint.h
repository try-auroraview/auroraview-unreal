#pragma once

#include "CoreMinimal.h"
#include "UObject/Object.h"
#include "SessionMailbox.h"
#include <memory>
#include "AuroraViewEndpoint.generated.h"

// The only object exposed through window.ue.auroraview. No editor API or eval.
UCLASS()
class UAuroraViewEndpoint final : public UObject
{
    GENERATED_BODY()
public:
    void Initialize(std::shared_ptr<AuroraView::SessionMailbox> InMailbox,
                    uint64 InGeneration, FString InToken);

    // Epic lowercases bound names: JavaScript calls postmessage, not PostMessage.
    UFUNCTION()
    bool PostMessage(const FString& Payload, const FString& SessionToken);

    // Separate reserved lifecycle admission; it never invokes a host handler.
    UFUNCTION()
    bool MarkReady(const FString& SessionToken);
private:
    // Assigned once before BindUObject, then immutable until UObject destruction.
    std::shared_ptr<AuroraView::SessionMailbox> Mailbox;
    uint64 Generation = 0;
    FString Token;
};
