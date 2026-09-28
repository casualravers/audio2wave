@echo off
rem Lance audio2wave_ridge.py --gui: double-clic, aucun terminal a connaitre.
rem A la difference de audio2wave_snap.py, l'entree audio doit encore etre
rem passee au demarrage (-d) -- ce script liste les entrees disponibles et la
rem demande avant de lancer la fenetre.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python est introuvable sur cette machine.
    echo Installe-le depuis https://www.python.org/downloads/ ^(coche "Add
    echo python.exe to PATH" pendant l'installation^), puis relance ce fichier.
    pause
    exit /b 1
)

echo Entrees audio disponibles :
echo.
python audio2wave_ridge.py --list-devices
echo.
set /p DEVICE=Nom exact de l'entree audio (copie-colle depuis la liste ci-dessus) :

if "%DEVICE%"=="" (
    echo Aucune entree saisie, arret.
    pause
    exit /b 1
)

python audio2wave_ridge.py -d "%DEVICE%" --gui
if errorlevel 1 (
    echo.
    echo Le programme s'est arrete avec une erreur ^(voir ci-dessus^).
    pause
)
endlocal
