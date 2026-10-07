# Optional installed-UE validation. Each Editor phase has its own timeout.
# Output must be new: this script never modifies or deletes an existing project.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EngineRoot,
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][string]$Output,
    [ValidateRange(30, 86400)][int]$TimeoutSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-JsonFile {
    param([Parameter(Mandatory = $true)]$Value, [Parameter(Mandatory = $true)][string]$Path)
    $Value | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Assert-PackageFiles {
    param([string]$Root, $Hashes)
    $entries = @($Hashes.PSObject.Properties)
    if ($entries.Count -eq 0) { throw 'Build receipt contains no package file hashes' }
    $items = @(Get-ChildItem -LiteralPath $Root -Recurse -Force)
    if (((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        @($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
        throw 'Package must not contain links or junctions'
    }
    if (@($items | Where-Object { -not $_.PSIsContainer }).Count -ne $entries.Count) {
        throw 'Package file inventory does not match the build receipt'
    }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($entry in $entries) {
        $relative = $entry.Name
        $segments = $relative.Split('/')
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '[\\:<>"|?*\x00-\x1f]' -or
            @($segments | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' -or $_.TrimEnd('.', ' ') -cne $_ }).Count -or
            -not $seen.Add($relative) -or $entry.Value -notmatch '^[a-fA-F0-9]{64}$') {
            throw "Unsafe package path or hash in build receipt: $relative"
        }
        $path = [IO.Path]::GetFullPath((Join-Path $Root $relative))
        if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $entry.Value) {
            throw "Packaged file failed build receipt verification: $relative"
        }
    }
}

function Get-ReportCounter {
    param($Report, [string]$Name)
    $property = $Report.PSObject.Properties[$Name]
    if ($null -eq $property -or ($property.Value -isnot [int] -and $property.Value -isnot [long] -and
        $property.Value -isnot [double] -and $property.Value -isnot [decimal])) {
        throw "Automation report has no numeric '$Name' counter"
    }
    $value = [double]$property.Value
    if ([double]::IsNaN($value) -or [double]::IsInfinity($value) -or $value -lt 0 -or $value -ne [Math]::Floor($value)) {
        throw "Automation report has an invalid '$Name' counter"
    }
    return $value
}

function Assert-AutomationReport {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "No Automation index.json was produced: $Path"
    }
    # AutomationController serializes FAutomatedTestPassResults and
    # FAutomatedTestResult to this flat tests array; process exit is insufficient.
    $report = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    foreach ($counter in @('failed', 'notRun', 'inProcess')) {
        if ((Get-ReportCounter $report $counter) -ne 0) {
            throw "Automation report '$counter' is not zero"
        }
    }
    $successful = (Get-ReportCounter $report 'succeeded') + (Get-ReportCounter $report 'succeededWithWarnings')
    $expected = @(
        'AuroraView.Editor.BridgeRoundTrip',
        'AuroraView.Editor.DockedLifecycle',
        'AuroraView.Editor.DockFactoryReentrancy',
        'AuroraView.Editor.FailedDockRecovery',
        'AuroraView.Editor.TypedInspectorGuards',
        'AuroraView.Showcase.FixtureBridgeRoundTrip',
        'AuroraView.Showcase.FixtureTransform',
        'AuroraView.Showcase.NativeInteractionGuards'
    )
    $runtimeTest = Join-Path $fixturePlugin 'Source/AuroraViewEditor/Private/Tests/AuroraViewRuntimeControlTests.cpp'
    if (Test-Path -LiteralPath $runtimeTest -PathType Leaf) {
        $expected += 'AuroraView.Runtime.ControlReflection'
    }
    $testsProperty = $report.PSObject.Properties['tests']
    if ($null -eq $testsProperty -or $null -eq $testsProperty.Value) {
        throw 'Automation report contains no tests'
    }
    $tests = @($testsProperty.Value)
    if ($successful -ne $tests.Count -or $tests.Count -lt $expected.Count) {
        throw 'Automation success counters do not match the complete test list'
    }
    foreach ($test in $tests) {
        if ($test.state -cne 'Success' -or (Get-ReportCounter $test 'errors') -ne 0) {
            throw "Automation test did not succeed: $($test.fullTestPath)"
        }
    }
    foreach ($name in $expected) {
        $matches = @($tests | Where-Object { $_.fullTestPath -ceq $name })
        if ($matches.Count -ne 1) { throw "Expected exactly one successful test: $name" }
    }
    return $tests | Select-Object fullTestPath, state, warnings, errors, duration
}

function Invoke-FixtureEditor {
    param([string]$Executable, [string]$Arguments, [string]$Phase)
    $receiptPath = Join-Path $evidenceDirectory ($Phase + '-process.json')
    $receipt = [ordered]@{
        phase = $Phase
        executable = $Executable
        arguments = $Arguments
        startedUtc = [DateTime]::UtcNow.ToString('o')
        pid = $null
        exitCode = $null
        timedOut = $false
    }
    $process = $null
    try {
        $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $outputPath -WindowStyle Hidden -PassThru
        $receipt.pid = $process.Id
        Write-JsonFile $receipt $receiptPath
        Write-Host "$Phase started (PID $($process.Id)); log: $evidenceDirectory"
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $receipt.timedOut = $true
            throw "$Phase exceeded its $TimeoutSeconds second timeout"
        }
        # Refresh after waiting so ExitCode is populated on Windows PowerShell.
        $process.Refresh()
        $receipt.exitCode = $process.ExitCode
        if ($process.ExitCode -ne 0) { throw "$Phase exited with code $($process.ExitCode)" }
    }
    finally {
        if ($null -ne $process) {
            if (-not $process.HasExited) {
                # Only this invocation's live PID and descendants are targeted.
                # No shell, wildcard name, or enumeration of unrelated Editors.
                & (Join-Path $env:SystemRoot 'System32/taskkill.exe') /PID $process.Id /T /F | Out-Null
                $process.WaitForExit(10000) | Out-Null
            }
            $process.Refresh()
            if ($process.HasExited) { $receipt.exitCode = $process.ExitCode }
        }
        $receipt.completedUtc = [DateTime]::UtcNow.ToString('o')
        Write-JsonFile $receipt $receiptPath
        if ($null -ne $process) { $process.Dispose() }
    }
}

if ($env:OS -ne 'Windows_NT') { throw 'This validation entry point requires Windows and an installed supported Unreal Engine' }
$enginePath = (Resolve-Path -LiteralPath $EngineRoot).Path
$packagePath = (Resolve-Path -LiteralPath $Package).Path
$outputPath = [IO.Path]::GetFullPath($Output).TrimEnd('\', '/')
if (Test-Path -LiteralPath $outputPath) { throw 'Output already exists; choose a new isolated directory' }
foreach ($protected in @($enginePath, $packagePath, (Split-Path -Parent $PSScriptRoot))) {
    $prefix = $protected.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $outputPrefix = $outputPath + [IO.Path]::DirectorySeparatorChar
    if ($outputPath -ieq $protected -or $outputPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        $protected.StartsWith($outputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Output must be isolated from the source, engine and packaged plugin'
    }
}
$versionPath = Join-Path $enginePath 'Engine/Build/Build.version'
$version = Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json
$engineVersion = "$($version.MajorVersion).$($version.MinorVersion)"
if ($engineVersion -notin @('4.18', '4.26', '5.5', '5.7', '5.8')) { throw 'Engine is outside the supported validation matrix' }
$editorTarget = if ($version.MajorVersion -eq 4) { 'UE4Editor' } else { 'UnrealEditor' }
if (-not (Test-Path -LiteralPath (Join-Path $enginePath 'Engine/Build/InstalledBuild.txt') -PathType Leaf)) {
    throw 'EngineRoot must be an installed Unreal Engine build'
}
$preparationEditor = Join-Path $enginePath "Engine/Binaries/Win64/$editorTarget-Cmd.exe"
$renderingEditor = Join-Path $enginePath "Engine/Binaries/Win64/$editorTarget.exe"
foreach ($required in @($preparationEditor, $renderingEditor, (Join-Path $packagePath 'AuroraView.uplugin'),
    (Join-Path $packagePath "Binaries/Win64/$editorTarget-AuroraViewEditor.dll"),
    (Join-Path $packagePath "Binaries/Win64/$editorTarget-AuroraViewRuntime.dll"),
    (Join-Path $packagePath "Binaries/Win64/$editorTarget.modules"))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required packaged/installed file missing: $required" }
}
$buildReceiptPath = Join-Path (Split-Path $packagePath -Parent) 'build-receipt.json'
$buildReceiptHash = (Get-FileHash -LiteralPath $buildReceiptPath -Algorithm SHA256).Hash
$buildReceipt = Get-Content -LiteralPath $buildReceiptPath -Raw | ConvertFrom-Json
if ($buildReceipt.status -cne 'pass' -or $buildReceipt.unreal_compile -cne 'pass' -or
    (Resolve-Path -LiteralPath $buildReceipt.package.root).Path.TrimEnd('\', '/') -ine $packagePath.TrimEnd('\', '/')) {
    throw 'Package requires a passing build receipt bound to this exact directory'
}
$engineModulesPath = Join-Path $enginePath "Engine/Binaries/Win64/$editorTarget.modules"
$engineModules = Get-Content -LiteralPath $engineModulesPath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($engineModules.BuildId) -or
    $buildReceipt.engine.build_id -cne $engineModules.BuildId -or
    $buildReceipt.engine.version_sha256 -ine (Get-FileHash -LiteralPath $versionPath -Algorithm SHA256).Hash -or
    $buildReceipt.engine.modules_sha256 -ine (Get-FileHash -LiteralPath $engineModulesPath -Algorithm SHA256).Hash) {
    throw 'Build receipt engine identity differs from this installation'
}
if ($buildReceipt.source.commit -notmatch '^[a-fA-F0-9]{40,64}$' -or $buildReceipt.source.tree -notmatch '^[a-fA-F0-9]{40,64}$') {
    throw 'Build receipt has no valid source commit and tree identity'
}
Assert-PackageFiles $packagePath $buildReceipt.package.files_sha256
$dllRelative = "Binaries/Win64/$editorTarget-AuroraViewEditor.dll"
$dllHash = (Get-FileHash -LiteralPath (Join-Path $packagePath $dllRelative) -Algorithm SHA256).Hash
$packageModules = Get-Content -LiteralPath (Join-Path $packagePath "Binaries/Win64/$editorTarget.modules") -Raw | ConvertFrom-Json
if ($dllHash -ine $buildReceipt.package.dll.sha256 -or $packageModules.BuildId -cne $engineModules.BuildId) {
    throw 'Package DLL hash or BuildId differs from the passing build receipt'
}

# No Force: an existing output directory is never reused or overwritten.
New-Item -ItemType Directory -Path $outputPath | Out-Null
$evidenceDirectory = Join-Path $outputPath 'evidence'
New-Item -ItemType Directory -Path $evidenceDirectory | Out-Null
$resultPath = Join-Path $evidenceDirectory 'validation.json'
$result = [ordered]@{ status = 'failed'; engineRoot = $enginePath; package = $packagePath; output = $outputPath; startedUtc = [DateTime]::UtcNow.ToString('o') }
$result.build = [ordered]@{
    receipt = $buildReceiptPath; receipt_sha256 = $buildReceiptHash
    source_commit = $buildReceipt.source.commit; source_tree = $buildReceipt.source.tree
    source_dirty = $buildReceipt.source.dirty; source_working_files_sha256 = $buildReceipt.source.working_files_sha256
    dll_sha256 = $dllHash; engine_build_id = $engineModules.BuildId
    engine_version_sha256 = $buildReceipt.engine.version_sha256
}
try {
    Write-JsonFile $version (Join-Path $evidenceDirectory 'engine-version.json')
    $pluginsDirectory = Join-Path $outputPath 'Plugins'
    New-Item -ItemType Directory -Path $pluginsDirectory | Out-Null
    $fixturePlugin = Join-Path $pluginsDirectory 'AuroraView'
    Copy-Item -LiteralPath $packagePath -Destination $fixturePlugin -Recurse
    Assert-PackageFiles $packagePath $buildReceipt.package.files_sha256
    Assert-PackageFiles $fixturePlugin $buildReceipt.package.files_sha256
    $result.build.fixture_plugin = $fixturePlugin
    $fixtureProject = Join-Path $outputPath 'AuroraViewNativeFixture.uproject'
    $descriptor = [ordered]@{
        FileVersion = 3
        EngineAssociation = $engineVersion
        Category = 'Tests'
        Description = 'Disposable AuroraView native automation fixture'
        Plugins = @(
            [ordered]@{ Name = 'AuroraView'; Enabled = $true }
        )
    }
    Write-JsonFile $descriptor $fixtureProject
    $pythonPlugin = Join-Path $enginePath 'Engine/Plugins/Experimental/PythonScriptPlugin/PythonScriptPlugin.uplugin'
    $expectPython = Test-Path -LiteralPath $pythonPlugin -PathType Leaf
    if ($expectPython) {
        $descriptor.Plugins += [ordered]@{ Name = 'PythonScriptPlugin'; Enabled = $true }
        Write-JsonFile $descriptor $fixtureProject
    }
    $result.editor_python_fixture = if ($expectPython) { 'enabled' } else { 'unavailable' }
    $preparationLog = Join-Path $evidenceDirectory 'UnrealEditor-prepare.log'
    $preparationArguments = '"' + $fixtureProject + '" -run=AuroraViewPrepare -Map=/Game/AuroraViewAcceptance/Smoke -Unattended -NoSplash -NoSound -NoP4 -NullRHI -abslog="' + $preparationLog + '"'
    Invoke-FixtureEditor $preparationEditor $preparationArguments 'prepare'
    $mapFile = Join-Path $outputPath 'Content/AuroraViewAcceptance/Smoke.umap'
    $fixtureReceipt = Join-Path $evidenceDirectory 'fixture.json'
    if (-not (Test-Path -LiteralPath $mapFile -PathType Leaf) -or -not (Test-Path -LiteralPath $fixtureReceipt -PathType Leaf)) {
        throw 'Preparation did not produce both the saved fixture map and its receipt'
    }
    $prepared = Get-Content -LiteralPath $fixtureReceipt -Raw | ConvertFrom-Json
    if ($prepared.saved -ne $true -or $prepared.map -cne '/Game/AuroraViewAcceptance/Smoke' -or
        [IO.Path]::GetFullPath($prepared.project) -ine $outputPath) {
        throw 'Preparation receipt does not identify this isolated fixture'
    }

    $reportDirectory = Join-Path $evidenceDirectory 'automation'
    $acceptanceLog = Join-Path $evidenceDirectory 'UnrealEditor-automation.log'
    # Keep rendering enabled: the suite exercises actual CEF and Slate widgets.
    $automationArguments = '"' + $fixtureProject + '" /Game/AuroraViewAcceptance/Smoke -AuroraViewFixtureMap=/Game/AuroraViewAcceptance/Smoke -AuroraViewAllowFixtureMutations -AuroraViewAllowControl -Unattended -NoSplash -NoSound -NoP4 -Windowed -ResX=1280 -ResY=720 -ExecCmds="Automation RunTests AuroraView." -TestExit="Automation Test Queue Empty" -ReportExportPath="' + $reportDirectory + '" -abslog="' + $acceptanceLog + '"'
    if (-not $engineVersion.StartsWith('4.')) { $automationArguments += ' -LiveCoding=False' }
    if ($expectPython) { $automationArguments += ' -AuroraViewExpectEditorPython' }
    Assert-PackageFiles $packagePath $buildReceipt.package.files_sha256
    Assert-PackageFiles $fixturePlugin $buildReceipt.package.files_sha256
    Invoke-FixtureEditor $renderingEditor $automationArguments 'automation'
    $result.tests = @(Assert-AutomationReport (Join-Path $reportDirectory 'index.json'))
    if ((Get-FileHash -LiteralPath $buildReceiptPath -Algorithm SHA256).Hash -ine $buildReceiptHash -or
        (Get-FileHash -LiteralPath (Join-Path $packagePath $dllRelative) -Algorithm SHA256).Hash -ine $dllHash -or
        (Get-FileHash -LiteralPath (Join-Path $fixturePlugin $dllRelative) -Algorithm SHA256).Hash -ine $dllHash) {
        throw 'Build receipt or verified DLL changed during Editor validation'
    }
    $result.build.dll_sha256_after = $dllHash
    $result.status = 'passed'
    $result.scope = 'Native Automation tests; manual docking, drag and timestamped media acceptance remain separate'
}
catch {
    $result.error = $_.Exception.Message
    throw
}
finally {
    $result.completedUtc = [DateTime]::UtcNow.ToString('o')
    Write-JsonFile $result $resultPath
}
Write-Host "Native Editor automation passed. Evidence: $evidenceDirectory"
