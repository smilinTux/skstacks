# skwire installer (Windows).  Run in PowerShell:
#   irm <url>/install.ps1 | iex
# Installs the native skwire.exe from the latest release, or falls back to the
# universal skwire.pyz (needs Python 3.10+ on PATH).
$ErrorActionPreference = "Stop"
$repo   = if ($env:SKWIRE_REPO) { $env:SKWIRE_REPO } else { "smilinTux/skstacks" }
$binDir = if ($env:SKWIRE_BIN)  { $env:SKWIRE_BIN  } else { "$env:LOCALAPPDATA\skwire" }
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$latest = "https://github.com/$repo/releases/latest/download"

try {
  Invoke-WebRequest "$latest/skwire-windows-x64.exe" -OutFile "$binDir\skwire.exe"
  Write-Host "OK installed native binary -> $binDir\skwire.exe"
} catch {
  Invoke-WebRequest "$latest/skwire.pyz" -OutFile "$binDir\skwire.pyz"
  Set-Content "$binDir\skwire.cmd" "@echo off`r`npython `"%~dp0skwire.pyz`" %*"
  Write-Host "OK installed skwire.pyz (needs Python 3.10+) -> $binDir\skwire.cmd"
}
Write-Host "Add to PATH if needed:  $binDir"
Write-Host "Try:  skwire            (opens the chat-style setup)"
Write-Host "NOTE: skwire runs natively on Windows; the things it *deploys* (Docker/Swarm,"
Write-Host "      the *arr media stack, OpenBao) target Linux/containers — deploy to WSL2"
Write-Host "      or a remote Linux host (skwire asks 'deploy here or elsewhere?')."
