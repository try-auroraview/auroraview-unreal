#pragma once
#include "AuroraViewRuntimeModule.h"

// Parent IPC v1 transport plus negotiated AuroraView RPC/tool events.
// Socket IO, tool dispatch and UObject access stay on the owning GameThread.
class FAuroraViewControlHost final
{
public:
    explicit FAuroraViewControlHost(FAuroraViewRuntimeModule& InModule);
    ~FAuroraViewControlHost();
    void Start();
    void Tick();
    void Stop();
    bool RegisterTool(const FString& Name, FAuroraViewHandler Handler, const TSharedPtr<FJsonObject>& Description);
    bool UnregisterTool(const FString& Name);
    void Call(const FString& Method, const TSharedPtr<FJsonValue>& Params, FAuroraViewCompletion Complete);
    void Broadcast(const FString& Event, const TSharedPtr<FJsonValue>& Detail);
private:
    struct FImpl;
    TUniquePtr<FImpl> Impl;
};
