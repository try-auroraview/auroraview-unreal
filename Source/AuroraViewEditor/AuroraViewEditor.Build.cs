using UnrealBuildTool;
using System.IO;

public class AuroraViewEditor : ModuleRules
{
    public AuroraViewEditor(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        if (Target.Type != TargetType.Editor || Target.Platform != UnrealTargetPlatform.Win64)
        {
            throw new BuildException("AuroraView's current source candidate targets Win64 Editor only.");
        }
        // A source validation boundary, not a binary compatibility claim.
        if (Target.Version.MajorVersion != 5 || Target.Version.MinorVersion != 7)
        {
            throw new BuildException("This experimental source candidate permits UE 5.7 validation only. UHT/UBT and native Editor acceptance remain required.");
        }
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Json" });
        PrivateDependencyModuleNames.AddRange(new[] {
            "Engine", "UnrealEd", "Slate", "SlateCore", "WebBrowser", "Projects"
        });
        foreach (string RelativeFile in new[] {
            "Resources/ue_transport.js", "Resources/ue_bootstrap.js", "Resources/demo.html",
            "ThirdParty/AuroraViewCore/event_bridge.js",
            "ThirdParty/AuroraViewCore/bridge_stub.js",
            "ThirdParty/AuroraViewCore/LICENSE",
            "ThirdParty/AuroraViewCore/manifest.json"
        })
        {
            RuntimeDependencies.Add(Path.Combine(PluginDirectory, RelativeFile), StagedFileType.NonUFS);
        }
    }
}
