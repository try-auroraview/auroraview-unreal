using UnrealBuildTool;
using System.IO;
public class AuroraViewEditor : ModuleRules {
    public AuroraViewEditor(ReadOnlyTargetRules Target) : base(Target) {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PrivateIncludePaths.Add(Path.Combine(ModuleDirectory, "Private"));
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Json", "AuroraViewRuntime" });
        PrivateDependencyModuleNames.AddRange(new[] { "Engine", "UnrealEd", "Slate", "SlateCore", "WebBrowser", "Projects", "ContentBrowser", "SceneOutliner", "InputCore", "AssetRegistry" });
    }
}
