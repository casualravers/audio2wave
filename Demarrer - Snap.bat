@echo off
rem Lance audio2wave_snap.py --gui: double-clic, aucun terminal ni argument a
rem connaitre. L'entree audio se choisit dans la fenetre (menu deroulant
rem "Entree audio"), pas besoin de la preciser ici.
rem Cette fenetre a des boutons "Live"/"Ridge" pour basculer vers les deux
rem autres modes SANS jamais ouvrir de nouvelle fenetre: les reglages du
rem mode suivant se chargent DANS cette meme fenetre (meme entree audio
rem conservee) -- pas besoin des .bat dedies pour changer de mode en cours
rem de route, ils servent juste a demarrer directement sur un autre mode.
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

python audio2wave_snap.py --gui
if errorlevel 1 (
    echo.
    echo Le programme s'est arrete avec une erreur ^(voir ci-dessus^).
    pause
)
endlocal
