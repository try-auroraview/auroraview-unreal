#pragma once

#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <utility>

namespace AuroraView
{
// UE-independent transport ownership, used directly by the native adapter.
// This does not implement RPC, promises, or a second WebView engine.
class SessionMailbox final
{
public:
    enum class State { Closed, Opening, Visible, Hidden, Stopped };
    enum class Kind { Wire, Loaded, LoadError, Ready };
    struct Message { std::uint64_t Generation; std::string Payload; Kind Type = Kind::Wire; };
    static constexpr std::size_t MaxMessages = 256;
    static constexpr std::size_t MaxBytes = 65536;

    std::uint64_t Open()
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (Current != State::Closed) return 0;
        ++Generation;
        Current = State::Opening;
        return Generation;
    }
    bool Push(std::uint64_t Epoch, std::string Payload)
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (!Accepts(Epoch) || Payload.size() > MaxBytes || Queue.size() >= MaxMessages) return false;
        Queue.push_back({Epoch, std::move(Payload)});
        return true;
    }
    bool PushControl(std::uint64_t Epoch, Kind Type)
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (!Accepts(Epoch) || Type == Kind::Wire) return false;
        // Lifecycle signals are coalesced in reserved slots, never compete with
        // business RPC capacity, and cannot be lost under startup backpressure.
        switch (Type)
        {
        case Kind::Loaded: Loaded = true; break;
        case Kind::LoadError: LoadError = true; break;
        case Kind::Ready: Ready = true; break;
        default: return false;
        }
        return true;
    }
    std::deque<Message> Drain()
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        std::deque<Message> Result;
        // Failure takes priority and will invalidate the later wire messages.
        if (LoadError) Result.push_back({Generation, {}, Kind::LoadError});
        if (Loaded) Result.push_back({Generation, {}, Kind::Loaded});
        if (Ready) Result.push_back({Generation, {}, Kind::Ready});
        Loaded = LoadError = Ready = false;
        while (!Queue.empty())
        {
            Result.push_back(std::move(Queue.front()));
            Queue.pop_front();
        }
        return Result;
    }
    bool IsCurrent(std::uint64_t Epoch) const
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        return Accepts(Epoch);
    }
    void SetVisible(bool Visible)
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (Current != State::Closed && Current != State::Stopped)
            Current = Visible ? State::Visible : State::Hidden;
    }
    void Close()
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        Queue.clear();
        Loaded = LoadError = Ready = false;
        if (Current != State::Stopped) Current = State::Closed;
    }
    void Stop()
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        Queue.clear();
        Loaded = LoadError = Ready = false;
        Current = State::Stopped;
    }
    State GetState() const
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        return Current;
    }
private:
    bool Accepts(std::uint64_t Epoch) const
    {
        return Epoch == Generation && Current != State::Closed && Current != State::Stopped;
    }
    mutable std::mutex Mutex;
    State Current = State::Closed;
    std::uint64_t Generation = 0;
    bool Loaded = false;
    bool LoadError = false;
    bool Ready = false;
    std::deque<Message> Queue;
};
}
