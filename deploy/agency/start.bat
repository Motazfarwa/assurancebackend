@echo off
rem Serveur de l'agence : l'application (port 8000, reseau local) et la synchronisation avec Render.
rem Lance automatiquement au demarrage du PC par la tache planifiee "Assurance agence" (setup.ps1).
cd /d "%~dp0..\.."
if not exist deploy\agency\logs mkdir deploy\agency\logs
start "Synchronisation" /b cmd /c ".venv\Scripts\python.exe -m sync.worker >> deploy\agency\logs\sync.log 2>&1"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> deploy\agency\logs\server.log 2>&1
