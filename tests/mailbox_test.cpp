#include "SessionMailbox.h"
#include <atomic>
#include <cassert>
#include <iostream>
#include <thread>
#include <vector>

using AuroraView::SessionMailbox;

int main()
{
    int Cases = 0;
    {
        SessionMailbox M;
        assert(M.GetState() == SessionMailbox::State::Closed);
        assert(!M.Push(0, "closed"));
        auto G = M.Open();
        assert(G != 0 && M.Open() == 0);
        assert(M.Push(G, "opening"));
        M.SetVisible(true);
        assert(M.GetState() == SessionMailbox::State::Visible);
        M.SetVisible(false);
        assert(M.GetState() == SessionMailbox::State::Hidden);
        assert(M.Push(G, "hidden stays alive"));
        M.SetVisible(true);
        assert(M.Drain().size() == 2);
        ++Cases;
    }
    {
        SessionMailbox M;
        auto Old = M.Open();
        M.Push(Old, "must not execute");
        M.Close(); M.Close();
        assert(M.Drain().empty());
        auto Fresh = M.Open();
        assert(Fresh > Old);
        assert(!M.Push(Old, "late delivery"));
        assert(!M.IsCurrent(Old));
        assert(M.Push(Fresh, "new document"));
        ++Cases;
    }
    {
        SessionMailbox M;
        auto Old = M.Open();
        M.Push(Old, "already drained");
        auto Snapshot = M.Drain();
        M.Close(); M.Open();
        assert(!M.IsCurrent(Snapshot.front().Generation));
        ++Cases;
    }
    {
        SessionMailbox A, B;
        auto GA = A.Open(), GB = B.Open();
        A.Push(GA, "a"); B.Push(GB, "b");
        A.Close();
        assert(A.Drain().empty());
        assert(B.Drain().front().Payload == "b");
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        assert(M.Push(G, std::string(SessionMailbox::MaxBytes, 'x')));
        assert(!M.Push(G, std::string(SessionMailbox::MaxBytes + 1, 'x')));
        M.Drain();
        for (std::size_t i = 0; i < SessionMailbox::MaxMessages; ++i) assert(M.Push(G, "x"));
        assert(!M.Push(G, "overflow"));
        assert(M.Drain().size() == SessionMailbox::MaxMessages);
        assert(M.Push(G, "capacity recovered"));
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        assert(M.PushControl(G, SessionMailbox::Kind::Loaded));
        assert(!M.PushControl(G, SessionMailbox::Kind::Wire));
        assert(M.Drain().front().Type == SessionMailbox::Kind::Loaded);
        M.Close();
        assert(!M.PushControl(G, SessionMailbox::Kind::LoadError));
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        M.Push(G, "queued during exit");
        M.Stop(); M.Close(); M.Stop();
        assert(M.GetState() == SessionMailbox::State::Stopped);
        assert(M.Open() == 0 && !M.Push(G, "late"));
        assert(M.Drain().empty());
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        for (std::size_t i = 0; i < SessionMailbox::MaxMessages; ++i) assert(M.Push(G, "business"));
        assert(M.PushControl(G, SessionMailbox::Kind::Ready));
        assert(M.PushControl(G, SessionMailbox::Kind::Ready));
        assert(M.PushControl(G, SessionMailbox::Kind::Loaded));
        assert(M.PushControl(G, SessionMailbox::Kind::LoadError));
        auto Work = M.Drain();
        assert(Work.size() == SessionMailbox::MaxMessages + 3);
        assert(Work[0].Type == SessionMailbox::Kind::LoadError);
        assert(Work[1].Type == SessionMailbox::Kind::Loaded);
        assert(Work[2].Type == SessionMailbox::Kind::Ready);
        M.Close();
        for (const auto& Message : Work) assert(!M.IsCurrent(Message.Generation));
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        M.PushControl(G, SessionMailbox::Kind::Ready);
        M.Close();
        auto Fresh = M.Open();
        assert(Fresh > G && M.Drain().empty());
        assert(!M.PushControl(G, SessionMailbox::Kind::Ready));
        M.PushControl(Fresh, SessionMailbox::Kind::Ready);
        M.Stop();
        assert(M.Drain().empty());
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        std::vector<std::thread> Workers;
        std::atomic<int> Accepted{0};
        for (int i = 0; i < 8; ++i) Workers.emplace_back([&]() {
            for (int j = 0; j < 256; ++j) if (M.Push(G, "worker")) ++Accepted;
        });
        for (auto& Worker : Workers) Worker.join();
        assert(Accepted == SessionMailbox::MaxMessages);
        assert(M.Drain().size() == SessionMailbox::MaxMessages);
        ++Cases;
    }
    {
        SessionMailbox M;
        auto G = M.Open();
        std::atomic<bool> Start{false};
        std::thread Worker([&]() {
            while (!Start.load()) {}
            for (int i = 0; i < 10000; ++i) M.Push(G, "racing close");
        });
        Start = true; M.Close(); Worker.join();
        assert(M.Drain().empty() && !M.IsCurrent(G));
        ++Cases;
    }
    std::cout << "PASS: " << Cases << " native mailbox lifecycle/concurrency cases\n";
}
