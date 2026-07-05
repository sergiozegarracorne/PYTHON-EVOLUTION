@echo off
REM Compila webhook_server_backend.py como un .exe portable (todo en un solo
REM archivo). Esta es la version que va SOLO en la PC con VPN/acceso a
REM Evolution API. El resultado queda en dist\BackendEmergencias.exe

python -m PyInstaller --onefile --name BackendEmergencias --console ^
  --hidden-import=pyttsx3.drivers --hidden-import=pyttsx3.drivers.sapi5 ^
  --hidden-import=pystray._win32 ^
  --hidden-import=websockets.sync.client ^
  --hidden-import=PIL._tkinter_finder ^
  webhook_server_backend.py

echo.
echo ========================================================
echo Listo. El ejecutable quedo en dist\BackendEmergencias.exe
echo IMPORTANTE: copia tambien contactos.csv y config.ini junto
echo al .exe si lo llevas a otra carpeta o PC (no quedan incluidos
echo adentro, y sin config.ini se pierde el grupo autorizado de SEDAPAL).
echo ========================================================
pause
