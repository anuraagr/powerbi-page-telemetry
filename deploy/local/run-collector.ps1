#!/usr/bin/env pwsh
# run-collector.ps1
# Wrapper for scheduling the collector via Windows Task Scheduler or any
# Windows-native scheduler. Pulls secrets from environment variables or
# the user's Credential Manager via Get-Secret (PSResourceGet's
# Microsoft.PowerShell.SecretManagement module).
#
# Usage (manual test, mock mode - no credentials required):
#   ./run-collector.ps1 -Mock
#
# Usage (manual test, live):
#   ./run-collector.ps1 -OutputDir C:\PowerBITelemetry
#
# Usage (Task Scheduler):
#   Program/script:    pwsh.exe
#   Arguments:         -NoProfile -ExecutionPolicy Bypass -File "C:\path\to\run-collector.ps1"
#   Start in:          C:\path\to\powerbi-page-telemetry\etl
[CmdletBinding()]
param(
    [string]$OutputDir = "$env:USERPROFILE\PowerBITelemetry",
    [string]$LogDir    = "$env:USERPROFILE\PowerBITelemetry\logs",
    [string]$RepoEtl   = (Resolve-Path (Join-Path $PSScriptRoot "..\..\etl")).Path,
    [switch]$Mock
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONIOENCODING = "utf-8"

New-Item -ItemType Directory -Force -Path $OutputDir, $LogDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logFile = Join-Path $LogDir "collector-$timestamp.log"

if (-not $Mock) {
    # Optional: pull secrets from the user's vault if not already in env vars.
    # Requires:  Install-Module Microsoft.PowerShell.SecretManagement, Microsoft.PowerShell.SecretStore
    # Then:      Set-Secret -Name pbi-tenant-id -Secret '<value>'   (x3)
    if (-not $env:PBI_TENANT_ID -and (Get-Module -ListAvailable Microsoft.PowerShell.SecretManagement)) {
        try {
            $env:PBI_TENANT_ID     = ConvertFrom-SecureString -SecureString (Get-Secret -Name pbi-tenant-id)     -AsPlainText
            $env:PBI_CLIENT_ID     = ConvertFrom-SecureString -SecureString (Get-Secret -Name pbi-client-id)     -AsPlainText
            $env:PBI_CLIENT_SECRET = ConvertFrom-SecureString -SecureString (Get-Secret -Name pbi-client-secret) -AsPlainText
        } catch {
            Write-Warning "Could not fetch secrets from Microsoft.PowerShell.SecretManagement vault: $_"
        }
    }

    if (-not $env:PBI_TENANT_ID -or -not $env:PBI_CLIENT_ID -or -not $env:PBI_CLIENT_SECRET) {
        Write-Error "PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET must be set as env vars or stored in the SecretManagement vault (pass -Mock to evaluate without credentials)."
        exit 2
    }
    $extraArgs = @()
} else {
    Write-Host "[mock] running collector against bundled sample data - no Power BI credentials required"
    $extraArgs = @("--mock")
}

$env:PBI_OUTPUT_DIR = $OutputDir
$collector = Join-Path $RepoEtl "collector.py"

Write-Host "Running: python $collector $($extraArgs -join ' ')  (output -> $OutputDir; log -> $logFile)"
& python $collector @extraArgs 2>&1 | Tee-Object -FilePath $logFile
$exit = $LASTEXITCODE

if ($exit -ne 0) {
    Write-Error "collector.py exited with code $exit - see $logFile"
    exit $exit
}

Write-Host "Done."
