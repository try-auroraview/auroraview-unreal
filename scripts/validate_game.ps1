# Cook, package and run an isolated native Game fixture. Requires a verified BuildPlugin package.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EngineRoot,
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][string]$Output,
    [ValidateRange(30, 86400)][int]$TimeoutSeconds = 1800,
    [string]$Python = 'python',
    [switch]$RenderedBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$pythonExe = (Get-Command -Name $Python -CommandType Application -ErrorAction Stop).Source
$arguments = @((Join-Path $PSScriptRoot 'validate_game.py'), '--engine-root', $EngineRoot,
    '--package', $Package, '--output', $Output, '--timeout', $TimeoutSeconds)
if ($RenderedBrowser) { $arguments += '--rendered-browser' }
& $pythonExe @arguments
if ($LASTEXITCODE -ne 0) { throw "Packaged Game validation failed with exit code $LASTEXITCODE; evidence: $Output" }
