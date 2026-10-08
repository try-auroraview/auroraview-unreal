#!/usr/bin/env pwsh
<#
.SYNOPSIS
Run source contracts and portable C++ tests on Windows. Does not build Unreal.
.EXAMPLE
./scripts/check.ps1 -Python C:/tools/python/python.exe -Compiler C:/tools/llvm/bin/clang++.exe
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$Compiler = 'clang++',
    [string]$Node = 'node'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

function Resolve-Executable([string]$Name) {
    $command = Get-Command -Name $Name -CommandType Application -ErrorAction Stop
    return $command.Source
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable exited with code $LASTEXITCODE"
    }
}

$root = Split-Path -Parent $PSScriptRoot
$pythonExe = Resolve-Executable $Python
$compilerExe = Resolve-Executable $Compiler
$nodeExe = Resolve-Executable $Node
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$buildName = 'auroraview-check-' + [Guid]::NewGuid().ToString('N')
$build = [System.IO.Path]::GetFullPath((Join-Path $tempRoot $buildName))
$createdBuild = $false

try {
    New-Item -ItemType Directory -Path $build -ErrorAction Stop | Out-Null
    $createdBuild = $true
    foreach ($test in @(
        'scripts/verify_source.py',
        'tests/preflight_test.py',
        'tests/build_plugin_test.py',
        'tests/validate_game_test.py',
        'tests/python_client_test.py',
        'tests/demo_tools_test.py',
        'tests/owner_dispatch_test.py',
        'tests/run_demo_test.py',
        'tests/package_demo_test.py',
        'tests/native_evidence_test.py',
        'tests/native_showcase_source_test.py'
    )) {
        Invoke-Checked $pythonExe @((Join-Path $root $test))
    }

    $include = Join-Path $root 'Source/AuroraViewRuntime/Private'
    $editorInclude = Join-Path $root 'Source/AuroraViewEditor/Private'
    foreach ($test in @('mailbox_test', 'native_interaction_guards_test')) {
        $executable = Join-Path $build ($test + '.exe')
        Invoke-Checked $compilerExe @(
            '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pedantic',
            ('-I' + $include), ('-I' + $editorInclude), (Join-Path $root "tests/$test.cpp"), '-o', $executable
        )
        Invoke-Checked $executable @()
    }
    Invoke-Checked $nodeExe @(
        '--test', (Join-Path $root 'tests/bridge.test.cjs'),
        (Join-Path $root 'tests/native_showcase_ui.test.cjs'),
        (Join-Path $root 'tests/live_demo_ui.test.cjs')
    )
    Invoke-Checked $pythonExe @((Join-Path $root 'scripts/preflight_engine.py'))
}
finally {
    if ($createdBuild -and (Test-Path -LiteralPath $build)) {
        $resolvedBuild = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $build).Path)
        $resolvedParent = [System.IO.Path]::GetDirectoryName($resolvedBuild)
        $expectedParent = $tempRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar)
        $item = Get-Item -LiteralPath $resolvedBuild -Force
        if (-not [System.IO.Path]::IsPathRooted($resolvedBuild) -or
            $resolvedBuild -ne $build -or $resolvedParent -ne $expectedParent -or
            $item.Name -ne $buildName -or
            ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to remove unexpected temporary directory: $resolvedBuild"
        }
        Remove-Item -LiteralPath $resolvedBuild -Recurse -Force
    }
}
