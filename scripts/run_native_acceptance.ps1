# Run only on an authorized host with an installed UE 5.7 Win64 build.
# Does not download Unreal, accept licenses, install plugins or edit other projects.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$EngineRoot,
    [Parameter(Mandatory=$true)][string]$Project,
    [Parameter(Mandatory=$true)][string]$EvidenceDirectory,
    [Parameter(Mandatory=$true)][ValidatePattern('^/Game/AuroraViewAcceptance/[A-Za-z0-9_/]+$')][string]$FixtureMap
)
$ErrorActionPreference = 'Stop'
$version = Get-Content (Join-Path $EngineRoot 'Engine/Build/Build.version') -Raw | ConvertFrom-Json
if ($version.MajorVersion -ne 5 -or $version.MinorVersion -ne 7) { throw 'UE 5.7 is required' }
if ([IO.Path]::GetFileName($Project) -ne 'AuroraViewNativeFixture.uproject') { throw 'Use the isolated fixture project' }
if (-not (Test-Path $Project -PathType Leaf)) { throw 'Fixture project not found' }
$mapFile = Join-Path (Split-Path $Project -Parent) ('Content/' + $FixtureMap.Substring('/Game/'.Length) + '.umap')
if (-not (Test-Path $mapFile -PathType Leaf)) { throw 'Save the explicit isolated fixture map first' }
$editor = Join-Path $EngineRoot 'Engine/Binaries/Win64/UnrealEditor.exe'
if (-not (Test-Path $editor -PathType Leaf)) { throw 'Installed graphical Editor not found' }
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null
$version | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $EvidenceDirectory 'engine-version.json')
# Run after creating/saving a new empty project map and copying the freshly
# built plugin package. No -NullRHI: this suite includes actual CEF widgets.
$args = @($Project, $FixtureMap, ('-AuroraViewFixtureMap=' + $FixtureMap), '-AuroraViewAllowFixtureMutations', '-AuroraViewAllowControl', '-NoSplash', '-Windowed',
    '-ExecCmds=Automation RunTests AuroraView.', '-TestExit=Automation Test Queue Empty',
    ('-ReportExportPath=' + (Join-Path $EvidenceDirectory 'automation')),
    ('-abslog=' + (Join-Path $EvidenceDirectory 'UnrealEditor-acceptance.log')))
& $editor @args
if ($LASTEXITCODE -ne 0) { throw "Editor exited with code $LASTEXITCODE; retain the complete log" }
Write-Output 'Editor exited normally. Inspect Automation reports; process exit alone is not acceptance.'
Write-Output 'Manual native docking, sidebar, drag, viewport and timestamped media cases remain required.'
