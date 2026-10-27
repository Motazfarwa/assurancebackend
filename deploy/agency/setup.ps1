<#
Installation du serveur de l'agence (mode hors ligne), a lancer UNE fois sur le PC serveur,
dans PowerShell ouvert "en tant qu'administrateur", AVEC internet :

    powershell -ExecutionPolicy Bypass -File deploy\agency\setup.ps1 -OnlineDatabaseUrl "postgresql://user:motdepasse@hote:5432/base"

Etapes : sauvegarde de la base locale actuelle, installation de la synchronisation sur la base en ligne (Render, OVH...), copie de la base
en ligne sur ce PC, environnement Python, build du frontend, pare-feu (reseau local) et demarrage automatique.
Voir deploy\agency\README.md.
#>
param(
    [Parameter(Mandatory = $true)] [Alias("RenderDatabaseUrl")] [string] $OnlineDatabaseUrl,  # URL PostgreSQL de la base en ligne (Render : "External Database URL")
    [string] $LocalDatabase = "assurance_agence",                 # nouvelle base locale (jamais une base existante)
    [string] $LocalUser = "postgres",
    [string] $LocalHost = "localhost",
    [int] $LocalPort = 5432,
    [string] $FrontendRepo = "",                                  # par defaut : le dossier assurancefrontend voisin
    [int] $Port = 8000
)

$ErrorActionPreference = "Stop"
$Backend = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Here = $PSScriptRoot
# $PSScriptRoot is still empty while Windows PowerShell 5 evaluates parameter defaults: the default is set here.
if (-not $FrontendRepo) { $FrontendRepo = Join-Path $Backend "..\assurancefrontend" }
function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }

# --- Prerequis ---------------------------------------------------------------------------------------------
$PgBin = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\pg_dump.exe" -ErrorAction SilentlyContinue |
    Sort-Object { [int]($_.Directory.Parent.Name -replace '\D', '') } -Descending | Select-Object -First 1
if ($null -eq $PgBin) { throw "PostgreSQL introuvable dans C:\Program Files\PostgreSQL : installez-le d'abord." }
$PgBin = $PgBin.Directory.FullName
if ($null -eq (Get-Command py -ErrorAction SilentlyContinue)) { throw "Python (commande py) introuvable : installez Python 3.12 ou plus." }
if ($null -eq (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Node.js (npm) introuvable : installez Node.js LTS." }
if (-not (Test-Path (Join-Path $FrontendRepo "package.json"))) { throw "Projet frontend introuvable : $FrontendRepo (parametre -FrontendRepo)." }

# Local PostgreSQL password: asked here, unless already given in the PGPASSWORD environment variable.
if (-not $env:PGPASSWORD) {
    $secure = Read-Host "Mot de passe PostgreSQL local de '$LocalUser'" -AsSecureString
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
$local = @("-h", $LocalHost, "-p", "$LocalPort", "-U", $LocalUser)
$OnlinePg = $OnlineDatabaseUrl -replace '^postgresql\+psycopg://', 'postgresql://' -replace '^postgres://', 'postgresql://'
$OnlineSqlAlchemy = $OnlinePg -replace '^postgresql://', 'postgresql+psycopg://'
$encodedPassword = [Uri]::EscapeDataString($env:PGPASSWORD)
$LocalSqlAlchemy = "postgresql+psycopg://${LocalUser}:${encodedPassword}@${LocalHost}:${LocalPort}/$LocalDatabase"

$exists = & "$PgBin\psql.exe" @local -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$LocalDatabase'"
if ($exists -eq "1") { throw "La base '$LocalDatabase' existe deja : choisissez un autre nom (-LocalDatabase) ou supprimez-la vous-meme apres verification." }

# --- 1. Sauvegarde des bases locales actuelles ---------------------------------------------------------------
Step "Sauvegarde des bases locales actuelles"
$backups = Join-Path $Here "backups"
New-Item -ItemType Directory -Force $backups | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$databases = & "$PgBin\psql.exe" @local -d postgres -tAc "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"
foreach ($db in $databases) {
    if ($db) {
        & "$PgBin\pg_dump.exe" @local -d $db -Fc -f (Join-Path $backups "$db-$stamp.dump")
        if ($LASTEXITCODE -ne 0) { throw "Sauvegarde de $db impossible." }
        Write-Host "  $db -> backups\$db-$stamp.dump"
    }
}

# --- 2. Environnement Python et fichier .env ------------------------------------------------------------------
Step "Environnement Python"
$venvPython = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { & py -m venv (Join-Path $Backend ".venv") }
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $Backend "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Installation des paquets Python impossible." }

$envFile = Join-Path $Backend ".env"
$frontendDir = Join-Path $Here "frontend"
if (Test-Path $envFile) { Copy-Item $envFile (Join-Path $backups "env-$stamp.txt") }  # previous settings, kept
$lines = if (Test-Path $envFile) { @(Get-Content $envFile) } else { @() }
function Set-EnvLine([string] $key, [string] $value) {
    $script:lines = @($script:lines | Where-Object { $_ -notmatch "^$key=" }) + "$key=$value"
}
Set-EnvLine "DATABASE_URL" $LocalSqlAlchemy
Set-EnvLine "REMOTE_DATABASE_URL" $OnlineSqlAlchemy
Set-EnvLine "FRONTEND_DIR" $frontendDir
if (-not ($lines | Where-Object { $_ -match "^AUTH_SECRET_KEY=.{32,}" })) {
    Set-EnvLine "AUTH_SECRET_KEY" (& $venvPython -c "import secrets; print(secrets.token_urlsafe(48))")
}
[IO.File]::WriteAllLines($envFile, [string[]] $lines, (New-Object Text.UTF8Encoding $false))  # UTF-8 without BOM
Write-Host "  .env mis a jour (base locale $LocalDatabase, base en ligne, frontend)."

# --- 3. Synchronisation installee sur la base en ligne, puis copie de celle-ci sur ce PC--------------------------------
Step "Installation de la synchronisation sur la base en ligne"
Push-Location $Backend
& $venvPython -m sync.worker --install remote
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Installation sur la base en ligne impossible (URL, mot de passe, acces internet ?)." }

Step "Copie de la base en ligne sur ce PC (peut prendre plusieurs minutes)"
$onlineDump = Join-Path $backups "online-$stamp.dump"
& "$PgBin\pg_dump.exe" -d $OnlinePg -Fc --no-owner --no-acl -f $onlineDump
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Copie de la base en ligne impossible." }
& "$PgBin\createdb.exe" @local $LocalDatabase
& "$PgBin\pg_restore.exe" @local -d $LocalDatabase --no-owner --no-acl $onlineDump
if ($LASTEXITCODE -ne 0) { Write-Warning "pg_restore a signale des avertissements (souvent des roles du serveur en ligne absents ici) : verifiez les messages ci-dessus." }

Step "Activation de la synchronisation sur ce PC"
& $venvPython -m sync.worker --install local
& $venvPython -m sync.worker --start-after-restore
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Initialisation de la synchronisation impossible." }
Pop-Location

# --- 4. Frontend de l'agence -----------------------------------------------------------------------------------
Step "Build du frontend de l'agence"
Push-Location $FrontendRepo
if (-not (Test-Path "node_modules")) { & npm ci }
& npm run build:agency -- $frontendDir
$built = $LASTEXITCODE
Pop-Location
if ($built -ne 0) { throw "Build du frontend impossible." }

# --- 5. Reseau local et demarrage automatique (administrateur) -------------------------------------------------
Step "Pare-feu (reseau local uniquement) et demarrage automatique"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    & (Join-Path $Here "install-startup.ps1") -Port $Port
} else {
    Write-Warning "PowerShell n'est pas ouvert en administrateur : pare-feu et demarrage automatique non installes."
    Write-Warning "Lancez dans un PowerShell administrateur :  powershell -ExecutionPolicy Bypass -File `"$Here\install-startup.ps1`""
}

$name = $env:COMPUTERNAME
Write-Host ""
Write-Host "Termine." -ForegroundColor Green
Write-Host "  Sur ce PC          : http://localhost:$Port"
Write-Host "  Sur les autres PC  : http://${name}:$Port"
Write-Host "  Ancien logiciel    : faites-le pointer sur la base '$LocalDatabase' de ce PC (voir README)."
