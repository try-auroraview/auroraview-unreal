# Native Unreal demo scene

This source template is copied to a disposable project by the repository's demo
launcher. Generated target files select the installed engine's build settings;
the template deliberately has no version-specific `Target.cs` files or checked-in
Unreal binaries. Install the matching built AuroraView plugin in the generated
project's `Plugins/AuroraView` directory.

The scene uses Unreal's installed Cube, BasicShapeMaterial, ambient cubemap and
font assets. Their files are not redistributed in this repository. Constructor
references and packaging settings include the required content in the cooked
Game. No third-party art, Python runtime or Editor module is required by the Game
scene.

`AAuroraViewDemoGameMode` spawns one `AAuroraViewDemoScene` on the Entry map and
sets each player's view to its fixed camera. The actor owns both cubes, floor,
backdrop, lighting, camera and labels. Cube A is cyan and responds to Python
commands; Cube B is orange and provides a stationary visual reference. The scene
has no tick animation, so movement is a direct result of the native command.

## Native control contract

Discover the unique actor by class
`/Script/AuroraViewGameFixture.AuroraViewDemoScene` and actor name
`AuroraViewDemoScene`. Use the returned object path instead of assuming a map or
PIE package name.

| Reflected function | Arguments | Result |
| --- | --- | --- |
| `SetCubeHeight` | `Height`: finite float, 0–300 cm | `bool`; false changes nothing |
| `GetDemoState` | none | `{Height, Revision, CubeLocation}` |
| `ResetScene` | none | State after returning the cyan cube to its resting position |

`Height` is the vertical lift offset, initially zero. The cube's actual relative
Z is `80 + Height`. `GetDemoState` reads the mesh transform; it does not echo the
requested value. Every accepted setter increments `Revision`, including reset.
The scene's native status label shows the same readback. Both functions are
available in Editor and cooked Runtime through Unreal reflection.

## Isolated Editor scene

The `AuroraView.Demo.Scene` console command exists only in Editor builds. It
requires the exact `AuroraViewGameFixture` project name and `-AuroraViewDemo`
command-line flag. It refuses PIE, authored maps and unsaved user changes. From
an unchanged Entry or blank startup map it creates a fresh unsaved map, spawns
the same actor and positions the perspective viewport. Repeating the command
reuses the owned scene. The demo never saves over an engine or user map.

The module depends on UnrealEd only when `Target.bBuildEditor` is true. Game
targets link only Core, CoreUObject and Engine. AuroraView owns communication and
the external Python tool lifecycle; this fixture owns its scene and native
functions.
