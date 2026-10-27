@echo off
rem Ouvre l'application : la version en ligne quand internet marche, sinon le serveur de l'agence.
rem Les deux versions ont les memes donnees (synchronisation). Copiez ce fichier sur le bureau de chaque PC.
rem A adapter :
rem   SERVER     : localhost sur le PC serveur, le nom du PC serveur sur les autres PC (ex. DESKTOP-58PC99S)
rem   ONLINE_APP : adresse de l'application en ligne (a changer lors du passage chez OVH)
rem   ONLINE_API : adresse du backend en ligne, testee pour savoir si internet et le serveur en ligne repondent
set SERVER=localhost
set ONLINE_APP=https://assurancefrontend.onrender.com/workspace
set ONLINE_API=https://assurancebackend.onrender.com

curl --silent --output nul --max-time 5 %ONLINE_API%/health
if %errorlevel%==0 (
    start "" "%ONLINE_APP%"
) else (
    start "" "http://%SERVER%:8000/workspace"
)
