# build-hook.ps1 — Build the C# NativeAOT hook binary and drop it into
# src/claude_recall/native/ as pip package_data.
#
# Runs locally; CI runs the equivalent inside .github/workflows/build-wheels.yml.
#
# Usage:
#   .\build-hook.ps1                    # Release, win-x64
#   .\build-hook.ps1 -Configuration Debug
#   .\build-hook.ps1 -SkipCopy          # build only, don't stage into package
#
# Requires:
#   - .NET 9 SDK
#   - MSVC C++ Build Tools with the x86.x64 component. See CONTRIBUTING.md.
#
# If MSVC isn't on PATH, this script auto-detects a VS 2022 BuildTools install
# and populates PATH/INCLUDE/LIB for the current PowerShell session before
# invoking `dotnet publish`. No elevation required.

[CmdletBinding()]
param(
    [ValidateSet('Release', 'Debug')]
    [string]$Configuration = 'Release',
    [switch]$SkipCopy,
    [string]$Runtime = 'win-x64'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = Join-Path $RepoRoot 'src/ClaudeRecall.Hook'
$NativeOutDir = Join-Path $RepoRoot 'src/claude_recall/native'

# -- Detect MSVC BuildTools if the linker isn't on PATH --------------------

function Find-MsvcBuildTools {
    $candidates = @(
        'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools',
        'C:\Program Files (x86)\Microsoft Visual Studio\2022\Community',
        'C:\Program Files (x86)\Microsoft Visual Studio\2022\Professional',
        'C:\Program Files (x86)\Microsoft Visual Studio\2022\Enterprise'
    )
    foreach ($c in $candidates) {
        $msvc = Join-Path $c 'VC\Tools\MSVC'
        if (Test-Path $msvc) {
            $latest = Get-ChildItem $msvc -Directory |
                Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
                Sort-Object { [version]$_.Name } -Descending |
                Select-Object -First 1
            if ($latest -and (Test-Path (Join-Path $latest.FullName 'lib\x64\libcmt.lib'))) {
                return $latest.FullName
            }
        }
    }
    return $null
}

function Find-WindowsSdk {
    $sdkBase = 'C:\Program Files (x86)\Windows Kits\10'
    if (-not (Test-Path $sdkBase)) { return $null }
    $latest = Get-ChildItem (Join-Path $sdkBase 'Include') -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        Select-Object -First 1
    if ($latest) {
        return [pscustomobject]@{
            Root    = $sdkBase
            Version = $latest.Name
        }
    }
    return $null
}

$haveLinker = $null -ne (Get-Command link.exe -ErrorAction SilentlyContinue)
if (-not $haveLinker) {
    $msvcDir = Find-MsvcBuildTools
    $sdk     = Find-WindowsSdk
    if (-not $msvcDir) {
        throw 'MSVC Build Tools with the x86.x64 component (libcmt.lib) not found. See CONTRIBUTING.md.'
    }
    if (-not $sdk) {
        throw 'Windows 10/11 SDK not found under "C:\Program Files (x86)\Windows Kits\10".'
    }
    Write-Host "Using MSVC at $msvcDir"
    Write-Host "Using SDK $($sdk.Version) at $($sdk.Root)"
    $env:PATH    = "$msvcDir\bin\Hostx64\x64;" + $env:PATH
    $env:INCLUDE = "$msvcDir\include;$($sdk.Root)\include\$($sdk.Version)\ucrt;$($sdk.Root)\include\$($sdk.Version)\um;$($sdk.Root)\include\$($sdk.Version)\shared"
    $env:LIB     = "$msvcDir\lib\x64;$($sdk.Root)\lib\$($sdk.Version)\ucrt\x64;$($sdk.Root)\lib\$($sdk.Version)\um\x64"
}

# -- Publish ---------------------------------------------------------------

if (-not (Test-Path $ProjectDir)) {
    throw "Project directory not found: $ProjectDir"
}

Write-Host ''
Write-Host "Publishing $Configuration/$Runtime ..." -ForegroundColor Cyan
# IlcUseEnvironmentalTools=true tells NativeAOT to use whatever link.exe is on
# PATH instead of running findvcvarsall.bat (which can't locate BuildTools
# installations that aren't registered with vswhere).
dotnet publish $ProjectDir -c $Configuration -r $Runtime --self-contained -nologo /p:IlcUseEnvironmentalTools=true
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$publishDir = Join-Path $ProjectDir "bin/$Configuration/net9.0/$Runtime/publish"

# -- Copy artifacts into package data -------------------------------------

if ($SkipCopy) {
    Write-Host "Published to $publishDir (copy skipped)." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $NativeOutDir)) {
    New-Item -ItemType Directory -Path $NativeOutDir | Out-Null
}

Copy-Item -Path (Join-Path $publishDir 'claude-recall-hook.exe') -Destination $NativeOutDir -Force
$sqliteDll = Join-Path $publishDir 'e_sqlite3.dll'
if (Test-Path $sqliteDll) {
    Copy-Item -Path $sqliteDll -Destination $NativeOutDir -Force
}

Write-Host ''
Write-Host "Staged into ${NativeOutDir}:" -ForegroundColor Green
Get-ChildItem $NativeOutDir | Format-Table Name, @{N='Size'; E={'{0:N1} MB' -f ($_.Length/1MB)}}
