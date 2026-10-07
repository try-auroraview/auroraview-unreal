#pragma once
#include "AuroraViewRuntimeModule.h"

namespace AuroraViewNativeControl
{
// The host calls these only on GameThread after explicit control opt-in.
FAuroraViewReply Execute(const FString& Method, const TSharedPtr<FJsonValue>& Params, bool bAllowControl);
bool HasEditorPython();
}
