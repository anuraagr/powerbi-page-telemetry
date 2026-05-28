#!/usr/bin/env pwsh
# Deploy the page-telemetry collector as an Azure Function.
#
# Usage:
#   ./deploy.ps1 -ResourceGroup my-rg -FunctionApp my-pbi-collector
#
# Prereqs:
#   - Azure CLI (`az login` done)
#   - Azure Functions Core Tools v4 (`func --version` >= 4.0.5390)
#   - An existing Function App on the Python (3.10+) runtime
#   - Key Vault with secrets: pbi-tenant-id, pbi-client-id, pbi-client-secret
#   - Storage account + container `page-telemetry`
#   - Function App's Managed Identity granted:
#       * Key Vault Secrets User on the vault
#       * Storage Blob Data Contributor on the storage account
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$ResourceGroup,
    [Parameter(Mandatory)] [string]$FunctionApp,
    [string]$CollectorPath = "../../etl/collector.py"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $PSCommandPath
Push-Location $ScriptDir
try {
    Write-Host "1/3  Copying collector.py from $CollectorPath …" -ForegroundColor Cyan
    Copy-Item -Force $CollectorPath ".\collector.py"

    Write-Host "2/3  Publishing to $FunctionApp …" -ForegroundColor Cyan
    func azure functionapp publish $FunctionApp --python

    Write-Host "3/3  Cleaning up local collector.py copy …" -ForegroundColor Cyan
    Remove-Item -Force ".\collector.py"

    Write-Host "" 
    Write-Host "Deployed. Verify with:" -ForegroundColor Green
    Write-Host "  az functionapp function show -g $ResourceGroup -n $FunctionApp --function-name daily_page_telemetry"
    Write-Host "  az functionapp logs tail -g $ResourceGroup -n $FunctionApp"
}
finally {
    Pop-Location
}
