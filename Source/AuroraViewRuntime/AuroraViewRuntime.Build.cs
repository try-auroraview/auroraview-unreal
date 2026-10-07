using UnrealBuildTool;
using System.IO;
public class AuroraViewRuntime : ModuleRules {
    public AuroraViewRuntime(ReadOnlyTargetRules Target) : base(Target) {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine", "Json", "Slate", "SlateCore" });
        PrivateDependencyModuleNames.AddRange(new[] { "JsonUtilities", "WebBrowser", "Projects", "Sockets", "Networking", "InputCore" });
        // ModuleDirectory exists on the legacy UBT API; PluginDirectory does not.
        string PluginRoot = Path.GetFullPath(Path.Combine(ModuleDirectory, "../.."));
        foreach (string RelativeFile in new[] {
            "Resources/ue_transport.js", "Resources/ue_bootstrap.js", "Resources/demo.html", "Resources/native_showcase.html",
            "Resources/legacy/event_bridge.js", "Resources/legacy/bridge_stub.js", "Resources/legacy/manifest.json",
            "ThirdParty/AuroraViewCore/event_bridge.js", "ThirdParty/AuroraViewCore/bridge_stub.js",
            "ThirdParty/AuroraViewCore/LICENSE", "ThirdParty/AuroraViewCore/manifest.json"
        }) RuntimeDependencies.Add(Path.Combine(PluginRoot, RelativeFile), StagedFileType.NonUFS);
    }
}
