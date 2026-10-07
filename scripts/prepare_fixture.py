"""Create the disposable map used by validate_editor.ps1 inside Unreal Editor."""

import json
from pathlib import Path

import unreal


def main():
    project = Path(unreal.Paths.project_dir()).resolve()
    if not (project / "AuroraViewNativeFixture.uproject").is_file():
        raise RuntimeError("Only the disposable AuroraViewNativeFixture project is allowed")

    map_path = "/Game/AuroraViewAcceptance/Smoke"
    map_file = project / "Content" / "AuroraViewAcceptance" / "Smoke.umap"
    if map_file.exists():
        raise RuntimeError("Refusing to replace an existing fixture map")

    # UE 5.7 LevelEditor/Public/LevelEditorSubsystem.h: NewLevel creates, saves
    # and loads a blank level. SaveCurrentLevel confirms the saved map is valid.
    levels = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not levels.new_level(map_path, False):
        raise RuntimeError("LevelEditorSubsystem.new_level failed")
    if not levels.save_current_level() or not map_file.is_file():
        raise RuntimeError("The fixture map was not saved")

    receipt = project / "evidence" / "fixture.json"
    receipt.write_text(
        json.dumps({"project": str(project), "map": map_path, "saved": True}, indent=2),
        encoding="utf-8",
    )
    unreal.log("AuroraView fixture map saved: " + map_path)


main()
