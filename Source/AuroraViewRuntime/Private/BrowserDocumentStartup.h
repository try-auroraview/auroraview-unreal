#pragma once

#include <mutex>

namespace AuroraView
{
// A browser object can exist before its initial main frame is usable. This
// per-presentation gate admits one blank page, then one owned document only.
class BrowserDocumentStartup final
{
public:
    enum class Document { Initial, Owned, Other };

    bool AllowNavigation(Document Target, bool MainFrame, bool Redirect)
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (!MainFrame || Redirect || Current == Phase::Closed) return false;
        if (Target == Document::Initial && Current == Phase::Initial && !InitialNavigationUsed)
        {
            InitialNavigationUsed = true;
            return true;
        }
        if (Target == Document::Owned && Current == Phase::Owned && !OwnedNavigationUsed)
        {
            OwnedNavigationUsed = true;
            return true;
        }
        return false;
    }

    bool BeginOwnedDocument(bool InitialDocumentLoaded)
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        if (!InitialDocumentLoaded || Current != Phase::Initial) return false;
        Current = Phase::Owned;
        return true;
    }

    void Close()
    {
        std::lock_guard<std::mutex> Guard(Mutex);
        Current = Phase::Closed;
    }

private:
    enum class Phase { Initial, Owned, Closed };
    std::mutex Mutex;
    Phase Current = Phase::Initial;
    bool InitialNavigationUsed = false;
    bool OwnedNavigationUsed = false;
};
}
