<#
Serveur de l'agence : ouvre le port pour le reseau local et demarre l'application a l'allumage du PC.
A lancer dans PowerShell ouvert "en tant qu'administrateur" (setup.ps1 le fait lui-meme quand il l'est) :

    powershell -ExecutionPolicy Bypass -File deploy\agency\install-startup.ps1
#>
param([int] $Port = 8000)

$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Ouvrez PowerShell en tant qu'administrateur pour lancer ce script." }

# Port de l'application, ouvert aux seuls PC du reseau local.
if (-not (Get-NetFirewallRule -DisplayName "Assurance agence $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Assurance agence $Port" -Direction Inbound -Protocol TCP -LocalPort $Port `
        -Profile Private, Domain -RemoteAddress LocalSubnet -Action Allow | Out-Null
}

# Application + synchronisation lancees au demarrage du PC, relancees si elles s'arretent.
$action = New-ScheduledTaskAction -Execute (Join-Path $PSScriptRoot "start.bat")
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Assurance agence" -Action $action -Trigger $trigger -Settings $settings `
    -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "Assurance agence"
Write-Host "Pare-feu et demarrage automatique installes ; application lancee (http://localhost:$Port)." -ForegroundColor Green
