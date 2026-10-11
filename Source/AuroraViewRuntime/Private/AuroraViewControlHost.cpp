#include "AuroraViewControlHost.h"
#include "AuroraViewNativeControl.h"
#include "AuroraViewCompatibility.h"
#include "Dom/JsonObject.h"
#include "HAL/PlatformMisc.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"
#include "IPAddress.h"
#include "Misc/CommandLine.h"
#include "Misc/EngineVersion.h"
#include "Misc/Guid.h"
#include "Misc/Parse.h"
#include "Policies/CondensedJsonPrintPolicy.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "SocketSubsystem.h"
#include "Sockets.h"

DEFINE_LOG_CATEGORY_STATIC(LogAuroraViewControl, Log, All);
namespace
{
constexpr int32 MaxFrame = 1024 * 1024;
constexpr int32 MaxClients = 8;
constexpr int32 MaxPending = 256;
constexpr double ReplyTimeout = 30.0;
const TCHAR* RpcEvent = TEXT("__auroraview_rpc");
const TCHAR* WireResultEvent = TEXT("__auroraview_call_result");
const TCHAR* NativeMethods[] = {TEXT("unreal.engine.info"), TEXT("unreal.world.list"), TEXT("unreal.actor.list"),
    TEXT("unreal.object.describe"), TEXT("unreal.object.get"), TEXT("unreal.object.set"), TEXT("unreal.object.call"),
    TEXT("unreal.console.execute"), TEXT("unreal.python.execute")};

FAuroraViewReply Failure(const TCHAR* Code, const FString& Message)
{ return FAuroraViewReply::Failure(TEXT("AuroraViewHostError"), Message, Code); }

bool Text(const TSharedPtr<FJsonObject>& Object, const TCHAR* Key, FString& Value)
{
    const auto* Field = Object.IsValid() ? Object->Values.Find(Key) : nullptr;
    if (!Field || !Field->IsValid() || (*Field)->Type != EJson::String) return false;
    Value = (*Field)->AsString();
    return !Value.IsEmpty() && Value.Len() <= 256;
}

TSharedPtr<FJsonObject> ObjectField(const TSharedPtr<FJsonObject>& Object, const TCHAR* Key)
{
    const auto* Value = Object.IsValid() ? Object->Values.Find(Key) : nullptr;
    return Value && Value->IsValid() && (*Value)->Type == EJson::Object ? (*Value)->AsObject() : nullptr;
}

TSharedRef<FJsonObject> EventFrame(const FString& Event, const TSharedPtr<FJsonValue>& Data)
{
    auto Frame = MakeShared<FJsonObject>();
    Frame->SetStringField(TEXT("type"), TEXT("event"));
    Frame->SetStringField(TEXT("event"), Event);
    Frame->SetField(TEXT("data"), Data.IsValid() ? Data : MakeShared<FJsonValueNull>());
    return Frame;
}

TSharedRef<FJsonObject> ReplyFrame(const FString& Id, const FAuroraViewReply& Reply)
{
    auto Payload = MakeShared<FJsonObject>();
    Payload->SetStringField(TEXT("id"), Id);
    Payload->SetBoolField(TEXT("ok"), Reply.bOk);
    if (Reply.bOk) Payload->SetField(TEXT("result"), Reply.Result.IsValid() ? Reply.Result : MakeShared<FJsonValueNull>());
    else
    {
        auto Error = MakeShared<FJsonObject>();
        Error->SetStringField(TEXT("name"), Reply.ErrorName);
        Error->SetStringField(TEXT("message"), Reply.ErrorMessage);
        Error->SetStringField(TEXT("code"), Reply.ErrorCode);
        if (Reply.ErrorData.IsValid()) Error->SetField(TEXT("data"), Reply.ErrorData);
        Payload->SetObjectField(TEXT("error"), Error);
    }
    return EventFrame(WireResultEvent, MakeShared<FJsonValueObject>(Payload));
}

bool BoundedJson(const TArray<uint8>& Bytes)
{
    // Reject invalid UTF-8 instead of silently replacing bytes in a method/token.
    for (int32 Index = 0; Index < Bytes.Num();)
    {
        const uint8 Lead = Bytes[Index++];
        if (Lead < 0x80) continue;
        int32 Count = 0;
        uint32 Codepoint = 0, Minimum = 0;
        if (Lead >= 0xc2 && Lead <= 0xdf) { Count = 1; Codepoint = Lead & 0x1f; Minimum = 0x80; }
        else if (Lead >= 0xe0 && Lead <= 0xef) { Count = 2; Codepoint = Lead & 0x0f; Minimum = 0x800; }
        else if (Lead >= 0xf0 && Lead <= 0xf4) { Count = 3; Codepoint = Lead & 0x07; Minimum = 0x10000; }
        else return false;
        if (Index + Count > Bytes.Num()) return false;
        for (int32 Part = 0; Part < Count; ++Part)
        {
            const uint8 Next = Bytes[Index++];
            if ((Next & 0xc0) != 0x80) return false;
            Codepoint = (Codepoint << 6) | (Next & 0x3f);
        }
        if (Codepoint < Minimum || Codepoint > 0x10ffff || (Codepoint >= 0xd800 && Codepoint <= 0xdfff)) return false;
    }
    int32 Depth = 0;
    bool Quoted = false, Escaped = false;
    for (uint8 Byte : Bytes)
    {
        if (Quoted) { if (Escaped) Escaped = false; else if (Byte == '\\') Escaped = true; else if (Byte == '"') Quoted = false; continue; }
        if (Byte == '"') Quoted = true;
        else if (Byte == '{' || Byte == '[') { if (++Depth > 64) return false; }
        else if (Byte == '}' || Byte == ']') { if (--Depth < 0) return false; }
    }
    return Depth == 0 && !Quoted;
}
}

struct FAuroraViewControlHost::FImpl
{
    struct FClient
    {
        uint64 Id = 0;
        FSocket* Socket = nullptr;
        TArray<uint8> Input, Output;
        TSet<FString> Seen;
        int32 OutputOffset = 0;
        double ConnectedAt = 0;
        bool bAuthenticated = false, bClosed = false, bClosing = false;
        double CloseDeadline = 0;
    };
    struct FTool { FAuroraViewHandler Handler; TSharedPtr<FJsonObject> Description; };
    struct FRemoteTool { uint64 Owner; TSharedPtr<FJsonObject> Description; };
    struct FPending { uint64 Owner; double Deadline; FAuroraViewCompletion Complete; };
    FAuroraViewRuntimeModule& Module;
    FSocket* Listener = nullptr;
    ISocketSubsystem* Sockets = nullptr;
    TArray<TSharedPtr<FClient>> Clients;
    TMap<FString, FTool> Tools;
    TMap<FString, FRemoteTool> RemoteTools;
    TMap<FString, FPending> Pending;
    FString Token;
    uint64 NextClient = 0;
    bool bStopping = false, bAllowControl = false, bExitRequested = false;
    int32 Port = 0;
    double ExitDeadline = 0;

    explicit FImpl(FAuroraViewRuntimeModule& InModule) : Module(InModule) {}
    ~FImpl() { Stop(); }

    TSharedPtr<FClient> Client(uint64 Id)
    {
        for (const auto& Item : Clients) if (Item->Id == Id && !Item->bClosed && Item->bAuthenticated) return Item;
        return nullptr;
    }

    bool Send(const TSharedPtr<FClient>& Client, const TSharedRef<FJsonObject>& Frame)
    {
        if (!Client.IsValid() || Client->bClosed) return false;
        FString Json;
        FJsonSerializer::Serialize(Frame, TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Json));
        FTCHARToUTF8 Bytes(*Json);
        if (Bytes.Length() + 1 > MaxFrame || Client->Output.Num() - Client->OutputOffset + Bytes.Length() + 1 > MaxFrame * 2)
        { Client->bClosed = true; return false; }
        if (Client->OutputOffset) { AuroraViewCompatibility::RemoveAtNoShrink(Client->Output, 0, Client->OutputOffset); Client->OutputOffset = 0; }
        Client->Output.Append(reinterpret_cast<const uint8*>(Bytes.Get()), Bytes.Length());
        Client->Output.Add('\n');
        return true;
    }

    TSharedRef<FJsonObject> Describe()
    {
        auto InfoReply = AuroraViewNativeControl::Execute(TEXT("unreal.engine.info"), nullptr, bAllowControl);
        auto Info = InfoReply.Result->AsObject();
        Info->SetStringField(TEXT("rpc_protocol"), TEXT("auroraview.unreal/1"));
        Info->SetNumberField(TEXT("port"), Port);
        TArray<TSharedPtr<FJsonValue>> Capabilities;
        for (const auto* Name : {TEXT("event"), TEXT("rpc"), TEXT("tools")}) Capabilities.Add(MakeShared<FJsonValueString>(Name));
        Info->SetArrayField(TEXT("capabilities"), Capabilities);
        return Info.ToSharedRef();
    }

    void Start()
    {
        bAllowControl = FParse::Param(FCommandLine::Get(), TEXT("AuroraViewAllowControl"));
        if (!FParse::Value(FCommandLine::Get(), TEXT("AuroraViewHostPort="), Port)) return;
        if (Port < 1 || Port > 65535 || !FParse::Value(FCommandLine::Get(), TEXT("AuroraViewHostToken="), Token) || Token.Len() < 32)
        { UE_LOG(LogAuroraViewControl, Error, TEXT("AuroraView host requires a valid port and a token of at least 32 characters")); Port = 0; return; }
        Sockets = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
        if (!Sockets) return;
        Listener = Sockets->CreateSocket(NAME_Stream, TEXT("AuroraView parent IPC"), false);
        auto Address = Sockets->CreateInternetAddr();
        bool bValid = false;
        Address->SetIp(TEXT("127.0.0.1"), bValid);
        Address->SetPort(Port);
        const TCHAR* FailureStage = nullptr;
        if (!Listener) FailureStage = TEXT("create_socket");
        else if (!bValid) FailureStage = TEXT("loopback_address");
        else if (!Listener->SetNonBlocking(true)) FailureStage = TEXT("nonblocking");
        else if (!Listener->Bind(*Address)) FailureStage = TEXT("bind");
        else if (!Listener->Listen(MaxClients)) FailureStage = TEXT("listen");
        if (FailureStage)
        {
            const auto SocketError = Sockets->GetLastErrorCode();
            if (Listener) { Listener->Close(); Sockets->DestroySocket(Listener); Listener = nullptr; }
            UE_LOG(LogAuroraViewControl, Error, TEXT("AuroraView loopback host startup failed: stage=%s port=%d socket_error=%d"),
                FailureStage, Port, static_cast<int32>(SocketError));
            return;
        }
        UE_LOG(LogAuroraViewControl, Display, TEXT("AuroraView parent IPC v1 ready; pid=%u port=%d context=%s"),
            FPlatformProcess::GetCurrentProcessId(), Port, GIsEditor ? TEXT("editor") : TEXT("game"));
    }

    void Hello(const TSharedPtr<FClient>& Client, const TSharedPtr<FJsonObject>& Frame)
    {
        FString Type, SuppliedToken;
        double Protocol = 0;
        const auto Data = ObjectField(Frame, TEXT("data"));
        const auto* TokenValue = Data.IsValid() ? Data->Values.Find(TEXT("token")) : nullptr;
        const bool bToken = TokenValue && TokenValue->IsValid() && (*TokenValue)->Type == EJson::String && (*TokenValue)->AsString() == Token;
        const auto* CapsValue = Data.IsValid() ? Data->Values.Find(TEXT("capabilities")) : nullptr;
        bool bRpc = false;
        if (CapsValue && CapsValue->IsValid() && (*CapsValue)->Type == EJson::Array)
            for (const auto& Cap : (*CapsValue)->AsArray()) if (Cap.IsValid() && Cap->Type == EJson::String && Cap->AsString() == TEXT("rpc")) bRpc = true;
        const bool bAccept = Text(Frame, TEXT("type"), Type) && Type == TEXT("hello")
            && Frame->TryGetNumberField(TEXT("protocol"), Protocol) && Protocol == 1 && bToken && bRpc;
        auto Ack = MakeShared<FJsonObject>();
        Ack->SetStringField(TEXT("type"), TEXT("hello_ack"));
        Ack->SetNumberField(TEXT("protocol"), 1);
        Ack->SetBoolField(TEXT("accepted"), bAccept);
        Ack->SetStringField(TEXT("parent_id"), FString::Printf(TEXT("unreal-%u"), FPlatformProcess::GetCurrentProcessId()));
        Ack->SetObjectField(TEXT("data"), Describe());
        Send(Client, Ack);
        Client->bAuthenticated = bAccept;
        if (!bAccept) { Client->bClosing = true; Client->CloseDeadline = FPlatformTime::Seconds() + 0.5; }
    }

    void Result(const TSharedPtr<FClient>& Client, const TSharedPtr<FJsonObject>& Payload)
    {
        FString Id;
        bool bOk = false;
        if (!Text(Payload, TEXT("id"), Id) || !Payload->TryGetBoolField(TEXT("ok"), bOk)) { Client->bClosed = true; return; }
        auto* Found = Pending.Find(Id);
        if (!Found || Found->Owner != Client->Id) return;
        auto Complete = MoveTemp(Found->Complete);
        Pending.Remove(Id);
        if (bOk)
        {
            const auto* Value = Payload->Values.Find(TEXT("result"));
            Complete(FAuroraViewReply::Success(Value ? *Value : MakeShared<FJsonValueNull>()));
        }
        else
        {
            const auto Error = ObjectField(Payload, TEXT("error"));
            FString Name = TEXT("PythonToolError"), Message = TEXT("Python tool failed"), Code = TEXT("PYTHON_TOOL_ERROR");
            if (Error.IsValid()) { Error->TryGetStringField(TEXT("name"), Name); Error->TryGetStringField(TEXT("message"), Message); Error->TryGetStringField(TEXT("code"), Code); }
            const auto* Data = Error.IsValid() ? Error->Values.Find(TEXT("data")) : nullptr;
            Complete(FAuroraViewReply::Failure(Name, Message, Code, Data ? *Data : TSharedPtr<FJsonValue>()));
        }
    }

    FAuroraViewReply RegisterRemote(const TSharedPtr<FClient>& Client, const TSharedPtr<FJsonObject>& Params)
    {
        const auto* Values = Params.IsValid() ? Params->Values.Find(TEXT("tools")) : nullptr;
        if (!Values || !Values->IsValid() || (*Values)->Type != EJson::Array || (*Values)->AsArray().Num() > 128)
            return Failure(TEXT("INVALID_PARAMS"), TEXT("tools must contain at most 128 descriptors"));
        TSet<FString> Names;
        for (const auto& Value : (*Values)->AsArray())
        {
            const auto Description = Value.IsValid() && Value->Type == EJson::Object ? Value->AsObject() : nullptr;
            FString Name;
            if (!Text(Description, TEXT("name"), Name) || Name.StartsWith(TEXT("auroraview.")) || Name.StartsWith(TEXT("unreal."))
                || Names.Contains(Name) || Tools.Contains(Name) || (RemoteTools.Contains(Name) && RemoteTools[Name].Owner != Client->Id))
                return Failure(TEXT("TOOL_CONFLICT"), TEXT("Tool names must be unique, nonreserved and owned by this connection"));
            Names.Add(Name);
        }
        int32 Added = 0;
        for (const auto& Name : Names) if (!RemoteTools.Contains(Name)) ++Added;
        if (RemoteTools.Num() + Added > 512) return Failure(TEXT("LIMIT"), TEXT("Host tool registry is full"));
        for (const auto& Value : (*Values)->AsArray())
        {
            FString Name;
            Text(Value->AsObject(), TEXT("name"), Name);
            RemoteTools.Add(Name, {Client->Id, Value->AsObject()});
        }
        return FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true));
    }

    void Frame(const TSharedPtr<FClient>& Client, const TSharedPtr<FJsonObject>& Frame)
    {
        if (!Client->bAuthenticated) { Hello(Client, Frame); return; }
        FString Type, Event;
        if (!Text(Frame, TEXT("type"), Type)) { Client->bClosed = true; return; }
        if (Type == TEXT("ping"))
        {
            auto Pong = MakeShared<FJsonObject>(); Pong->SetStringField(TEXT("type"), TEXT("pong")); Pong->SetNumberField(TEXT("protocol"), 1); Send(Client, Pong); return;
        }
        if (Type != TEXT("event") || !Text(Frame, TEXT("event"), Event)) return;
        const auto* Data = Frame->Values.Find(TEXT("data"));
        const auto Payload = Data && Data->IsValid() && (*Data)->Type == EJson::Object ? (*Data)->AsObject() : nullptr;
        if (Event == WireResultEvent) { if (Payload.IsValid()) Result(Client, Payload); else Client->bClosed = true; return; }
        if (Event == TEXT("child:ready")) return;
        if (Event == TEXT("child:closing")) { Client->bClosed = true; return; }
        if (Event != RpcEvent)
        {
            if (!Event.StartsWith(TEXT("__auroraview_"))) Module.BroadcastEvent(Event, Data ? *Data : MakeShared<FJsonValueNull>());
            return;
        }
        FString Id, Method, CallType;
        if (!Text(Payload, TEXT("id"), Id) || !Text(Payload, TEXT("type"), CallType) || CallType != TEXT("call") || !Text(Payload, TEXT("method"), Method))
        { Client->bClosed = true; return; }
        if (Client->Seen.Contains(Id)) return;
        if (Client->Seen.Num() >= 8192) { Send(Client, ReplyFrame(Id, Failure(TEXT("SESSION_LIMIT"), TEXT("Reconnect to start a new request ledger")))); return; }
        Client->Seen.Add(Id);
        const auto* Value = Payload->Values.Find(TEXT("params"));
        const auto Params = Value ? *Value : TSharedPtr<FJsonValue>();
        if (Method == TEXT("auroraview.tools.register"))
        { Send(Client, ReplyFrame(Id, RegisterRemote(Client, Params.IsValid() && Params->Type == EJson::Object ? Params->AsObject() : nullptr))); return; }
        if (Method == TEXT("auroraview.tools.unregister"))
        {
            const auto Object = Params.IsValid() && Params->Type == EJson::Object ? Params->AsObject() : nullptr;
            const auto* Names = Object.IsValid() ? Object->Values.Find(TEXT("names")) : nullptr;
            if (!Names || !Names->IsValid() || (*Names)->Type != EJson::Array) { Send(Client, ReplyFrame(Id, Failure(TEXT("INVALID_PARAMS"), TEXT("names must be an array")))); return; }
            for (const auto& Name : (*Names)->AsArray())
            {
                const auto* Tool = Name.IsValid() && Name->Type == EJson::String ? RemoteTools.Find(Name->AsString()) : nullptr;
                if (!Tool || Tool->Owner != Client->Id) { Send(Client, ReplyFrame(Id, Failure(TEXT("TOOL_CONFLICT"), TEXT("Only the registering connection can remove a tool")))); return; }
            }
            for (const auto& Name : (*Names)->AsArray()) RemoteTools.Remove(Name->AsString());
            Send(Client, ReplyFrame(Id, FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true)))); return;
        }
        TWeakPtr<FClient> Weak = Client;
        Call(Method, Params, [this, Weak, Id](FAuroraViewReply Reply) { const auto Owner = Weak.Pin(); if (Owner.IsValid() && !bStopping) Send(Owner, ReplyFrame(Id, Reply)); });
    }

    void Call(const FString& Method, const TSharedPtr<FJsonValue>& Params, FAuroraViewCompletion Complete)
    {
        if (bStopping) { Complete(Failure(TEXT("HOST_STOPPED"), TEXT("Host is stopping"))); return; }
        for (const auto* Native : NativeMethods) if (Method == Native) { Complete(AuroraViewNativeControl::Execute(Method, Params, bAllowControl)); return; }
        const auto Args = Params.IsValid() && Params->Type == EJson::Object ? Params->AsObject() : nullptr;
        if (Method == TEXT("auroraview.host.describe")) { Complete(FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Describe()))); return; }
        if (Method == TEXT("auroraview.tools.list"))
        {
            TArray<TSharedPtr<FJsonValue>> List;
            for (const auto* Name : NativeMethods) { auto D = MakeShared<FJsonObject>(); D->SetStringField(TEXT("name"), Name); D->SetBoolField(TEXT("enabled"), FString(Name) == TEXT("unreal.engine.info") || (bAllowControl && (FString(Name) != TEXT("unreal.python.execute") || AuroraViewNativeControl::HasEditorPython()))); List.Add(MakeShared<FJsonValueObject>(D)); }
            for (const auto& Pair : Tools) { auto D = Pair.Value.Description.IsValid() ? Pair.Value.Description : MakeShared<FJsonObject>(); D->SetStringField(TEXT("name"), Pair.Key); List.Add(MakeShared<FJsonValueObject>(D)); }
            for (const auto& Pair : RemoteTools) List.Add(MakeShared<FJsonValueObject>(Pair.Value.Description));
            Complete(FAuroraViewReply::Success(MakeShared<FJsonValueArray>(List))); return;
        }
        if (Method == TEXT("auroraview.host.shutdown"))
        {
            if (!bAllowControl) { Complete(Failure(TEXT("CONTROL_DISABLED"), TEXT("Host control is not enabled"))); return; }
            bExitRequested = true; ExitDeadline = FPlatformTime::Seconds() + 1;
            Complete(FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true))); return;
        }
        if (Method.StartsWith(TEXT("auroraview.view.")))
        {
            FString IdText;
            if (!Text(Args, TEXT("id"), IdText)) { Complete(Failure(TEXT("INVALID_PARAMS"), TEXT("View id is required"))); return; }
            const FName Id(*IdText);
            if (Method == TEXT("auroraview.view.open"))
            {
                FString Html, Title, Error;
                if (!Args->TryGetStringField(TEXT("html"), Html)) { Complete(Failure(TEXT("INVALID_PARAMS"), TEXT("Trusted html is required"))); return; }
                Args->TryGetStringField(TEXT("title"), Title);
                FString Presentation = TEXT("floating");
                if (Args->HasField(TEXT("presentation")) && !Text(Args, TEXT("presentation"), Presentation))
                { Complete(Failure(TEXT("INVALID_PARAMS"), TEXT("presentation must be floating or docked"))); return; }
                if (Presentation != TEXT("floating") && Presentation != TEXT("docked"))
                { Complete(Failure(TEXT("INVALID_PARAMS"), TEXT("presentation must be floating or docked"))); return; }
                if (Presentation == TEXT("docked") && (!GIsEditor || IsRunningCommandlet()))
                { Complete(Failure(TEXT("EDITOR_ONLY"), TEXT("Native dock tabs require an interactive Editor host"))); return; }
                const bool bOpened = Presentation == TEXT("docked")
                    ? Module.OpenDocked(Id, Html, FText::FromString(Title), Error)
                    : Module.Open(Id, Html, FText::FromString(Title), Error);
                Complete(bOpened ? FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(true)) : Failure(TEXT("VIEW_OPEN_FAILED"), Error)); return;
            }
            if (Method == TEXT("auroraview.view.describe"))
            {
                Complete(FAuroraViewReply::Success(MakeShared<FJsonValueObject>(Module.DescribeView(Id)))); return;
            }
            bool bResult = false;
            if (Method == TEXT("auroraview.view.close")) bResult = Module.Close(Id);
            else if (Method == TEXT("auroraview.view.remove")) bResult = Module.Remove(Id);
            else if (Method == TEXT("auroraview.view.show")) bResult = Module.Show(Id);
            else if (Method == TEXT("auroraview.view.hide")) bResult = Module.Hide(Id);
            else { Complete(Failure(TEXT("METHOD_NOT_FOUND"), TEXT("Unknown view operation"))); return; }
            Complete(FAuroraViewReply::Success(MakeShared<FJsonValueBoolean>(bResult))); return;
        }
        if (const auto* Found = Tools.Find(Method)) { const auto Handler = Found->Handler; Complete(Handler(Params)); return; }
        const auto* Remote = RemoteTools.Find(Method);
        if (!Remote) { Complete(Failure(TEXT("METHOD_NOT_FOUND"), TEXT("No registered native or Python tool"))); return; }
        const uint64 Owner = Remote->Owner;
        const auto Target = Client(Owner);
        if (!Target.IsValid() || Pending.Num() >= MaxPending) { Complete(Failure(TEXT("TOOL_UNAVAILABLE"), TEXT("Tool owner is disconnected or the pending request limit was reached"))); return; }
        const FString Id = FGuid::NewGuid().ToString(EGuidFormats::Digits);
        Pending.Add(Id, {Owner, FPlatformTime::Seconds() + ReplyTimeout, MoveTemp(Complete)});
        auto Call = MakeShared<FJsonObject>(); Call->SetStringField(TEXT("type"), TEXT("call")); Call->SetStringField(TEXT("id"), Id); Call->SetStringField(TEXT("method"), Method);
        if (Params.IsValid()) Call->SetField(TEXT("params"), Params);
        if (!Send(Target, EventFrame(RpcEvent, MakeShared<FJsonValueObject>(Call))))
        {
            auto* Found = Pending.Find(Id);
            if (Found) { auto Failed = MoveTemp(Found->Complete); Pending.Remove(Id); Failed(Failure(TEXT("TOOL_DISCONNECTED"), TEXT("Python tool output could not be queued"))); }
        }
    }

    void Disconnect(const TSharedPtr<FClient>& Client)
    {
        Client->bClosed = true;
        if (Client->Socket) { Client->Socket->Close(); Sockets->DestroySocket(Client->Socket); Client->Socket = nullptr; }
        TArray<FString> Remove;
        for (const auto& Pair : RemoteTools) if (Pair.Value.Owner == Client->Id) Remove.Add(Pair.Key);
        for (const auto& Name : Remove) RemoteTools.Remove(Name);
        Remove.Reset();
        for (const auto& Pair : Pending) if (Pair.Value.Owner == Client->Id) Remove.Add(Pair.Key);
        for (const auto& Id : Remove)
        {
            auto* Found = Pending.Find(Id);
            if (!Found) continue;
            auto Complete = MoveTemp(Found->Complete); Pending.Remove(Id);
            if (!bStopping) Complete(Failure(TEXT("TOOL_DISCONNECTED"), TEXT("Python tool owner disconnected")));
        }
    }

    void Tick()
    {
        if (bStopping) return;
        if (Listener)
        {
            bool bWaiting = false;
            for (int32 Count = 0; Count < 2 && Listener->HasPendingConnection(bWaiting) && bWaiting; ++Count)
            {
                FSocket* Socket = Listener->Accept(TEXT("AuroraView Python"));
                if (!Socket) break;
                if (Clients.Num() >= MaxClients) { Socket->Close(); Sockets->DestroySocket(Socket); continue; }
                Socket->SetNonBlocking(true);
#if ENGINE_MAJOR_VERSION >= 5 || ENGINE_MINOR_VERSION >= 26
                Socket->SetNoDelay(true);
#endif
                auto Item = MakeShared<FClient>(); Item->Id = ++NextClient; Item->Socket = Socket; Item->ConnectedAt = FPlatformTime::Seconds(); Clients.Add(Item);
            }
        }
        const auto Snapshot = Clients;
        for (const auto& Item : Snapshot)
        {
            if (!Item->bAuthenticated && FPlatformTime::Seconds() - Item->ConnectedAt > 5) Item->bClosed = true;
            if (Item->Socket && Item->Socket->GetConnectionState() != SCS_Connected) Item->bClosed = true;
            uint32 Available = 0;
            if (!Item->bClosed && !Item->bClosing && Item->Socket->HasPendingData(Available))
            {
                uint8 Buffer[65536]; int32 Read = 0;
                if (Item->Socket->Recv(Buffer, FMath::Min<int32>(Available, sizeof(Buffer)), Read) && Read > 0) Item->Input.Append(Buffer, Read);
            }
            else if (!Item->bClosed && !Item->bClosing)
            {
                uint8 Probe = 0; int32 Read = 0;
                const bool bReceived = Item->Socket->Recv(&Probe, 1, Read, ESocketReceiveFlags::Peek);
                // Unreal maps EWOULDBLOCK to success with zero bytes; EOF is false.
                if (!bReceived) Item->bClosed = true;
            }
            for (int32 Count = 0; !Item->bClosed && !Item->bClosing && Count < 8; ++Count)
            {
                int32 Newline = INDEX_NONE;
                if (!Item->Input.Find('\n', Newline)) break;
                if (Newline + 1 > MaxFrame) { Item->bClosed = true; break; }
                TArray<uint8> Bytes; Bytes.Append(Item->Input.GetData(), Newline); AuroraViewCompatibility::RemoveAtNoShrink(Item->Input, 0, Newline + 1);
                if (Bytes.Num() && Bytes.Last() == '\r') Bytes.RemoveAt(Bytes.Num() - 1);
                if (Bytes.Num() >= 3 && Bytes[0] == 0xef && Bytes[1] == 0xbb && Bytes[2] == 0xbf) AuroraViewCompatibility::RemoveAtNoShrink(Bytes, 0, 3);
                if (Bytes.Num() == 0) continue;
                if (!BoundedJson(Bytes)) { Item->bClosed = true; break; }
                FUTF8ToTCHAR Decoded(reinterpret_cast<const ANSICHAR*>(Bytes.GetData()), Bytes.Num());
                TSharedPtr<FJsonObject> Message;
                if (!FJsonSerializer::Deserialize(TJsonReaderFactory<>::Create(FString(Decoded.Length(), Decoded.Get())), Message) || !Message.IsValid())
                { Item->bClosed = true; break; }
                Frame(Item, Message);
            }
            if (Item->Input.Num() >= MaxFrame) Item->bClosed = true;
            if (!Item->bClosed && Item->Output.Num() > Item->OutputOffset)
            {
                int32 Sent = 0;
                if (Item->Socket->Send(Item->Output.GetData() + Item->OutputOffset, Item->Output.Num() - Item->OutputOffset, Sent)) Item->OutputOffset += Sent;
                else if (Sockets->GetLastErrorCode() != SE_EWOULDBLOCK) Item->bClosed = true;
                if (Item->OutputOffset == Item->Output.Num()) { Item->Output.Reset(); Item->OutputOffset = 0; }
            }
            if (Item->bClosing && (Item->Output.Num() == Item->OutputOffset || FPlatformTime::Seconds() >= Item->CloseDeadline)) Item->bClosed = true;
            if (Item->bClosed) Disconnect(Item);
        }
        Clients.RemoveAll([](const TSharedPtr<FClient>& Item) { return Item->bClosed; });
        TArray<FString> Expired;
        for (const auto& Pair : Pending) if (Pair.Value.Deadline <= FPlatformTime::Seconds()) Expired.Add(Pair.Key);
        for (const auto& Id : Expired) { auto* Found = Pending.Find(Id); if (!Found) continue; auto Complete = MoveTemp(Found->Complete); Pending.Remove(Id); Complete(Failure(TEXT("TOOL_TIMEOUT"), TEXT("Python tool exceeded its reply deadline"))); }
        if (bExitRequested)
        {
            bool bDrained = true;
            for (const auto& Item : Clients) if (Item->Output.Num() > Item->OutputOffset) bDrained = false;
            if (bDrained || FPlatformTime::Seconds() >= ExitDeadline) FPlatformMisc::RequestExit(false);
        }
    }

    void Stop()
    {
        if (bStopping) return;
        bStopping = true;
        auto Cancelled = MoveTemp(Pending); Pending.Reset();
        const auto Snapshot = Clients;
        for (const auto& Client : Snapshot) Disconnect(Client);
        Clients.Reset(); RemoteTools.Reset(); Tools.Reset();
        for (auto& Pair : Cancelled) Pair.Value.Complete(Failure(TEXT("HOST_STOPPED"), TEXT("Host stopped before the tool completed")));
        if (Listener) { Listener->Close(); Sockets->DestroySocket(Listener); Listener = nullptr; }
        Token.Empty();
    }
};

FAuroraViewControlHost::FAuroraViewControlHost(FAuroraViewRuntimeModule& Module) : Impl(MakeUnique<FImpl>(Module)) {}
FAuroraViewControlHost::~FAuroraViewControlHost() = default;
void FAuroraViewControlHost::Start() { check(IsInGameThread()); Impl->Start(); }
void FAuroraViewControlHost::Tick() { check(IsInGameThread()); Impl->Tick(); }
void FAuroraViewControlHost::Stop() { check(IsInGameThread()); Impl->Stop(); }
bool FAuroraViewControlHost::RegisterTool(const FString& Name, FAuroraViewHandler Handler, const TSharedPtr<FJsonObject>& Description)
{
    check(IsInGameThread());
    if (Impl->bStopping || Name.IsEmpty() || Name.Len() > 256 || !Handler || Name.StartsWith(TEXT("auroraview.")) || Name.StartsWith(TEXT("unreal.")) || Impl->Tools.Contains(Name) || Impl->RemoteTools.Contains(Name)) return false;
    Impl->Tools.Add(Name, {MoveTemp(Handler), Description}); return true;
}
bool FAuroraViewControlHost::UnregisterTool(const FString& Name) { check(IsInGameThread()); return Impl->Tools.Remove(Name) != 0; }
void FAuroraViewControlHost::Call(const FString& Method, const TSharedPtr<FJsonValue>& Params, FAuroraViewCompletion Complete) { check(IsInGameThread()); Impl->Call(Method, Params, MoveTemp(Complete)); }
void FAuroraViewControlHost::Broadcast(const FString& Event, const TSharedPtr<FJsonValue>& Detail)
{
    check(IsInGameThread());
    const auto Frame = EventFrame(Event, Detail);
    for (const auto& Client : Impl->Clients) if (Client->bAuthenticated) Impl->Send(Client, Frame);
}
