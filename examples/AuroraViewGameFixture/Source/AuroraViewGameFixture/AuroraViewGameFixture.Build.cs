using UnrealBuildTool;

public class AuroraViewGameFixture : ModuleRules
{
    public AuroraViewGameFixture(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new[] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new[] { "AuroraViewRuntime", "Json" });
        if (Target.bBuildEditor)
        {
            PrivateDependencyModuleNames.Add("UnrealEd");
        }
    }
}
