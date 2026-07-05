@echo off
REM Compila webhook_server.py como un .exe portable (todo en un solo archivo).
REM El resultado queda en dist\MonitorEmergencias.exe

python -m PyInstaller --onefile --name MonitorEmergencias --console ^
  --hidden-import=pyttsx3.drivers --hidden-import=pyttsx3.drivers.sapi5 ^
  --hidden-import=pystray._win32 ^
  --hidden-import=websockets.sync.client ^
  --hidden-import=PIL._tkinter_finder ^
  webhook_server.py

echo.
echo ========================================================
echo Listo. El ejecutable quedo en dist\MonitorEmergencias.exe
echo IMPORTANTE: copia tambien contactos.csv y config.ini junto
echo al .exe si lo llevas a otra carpeta o PC (no quedan incluidos
echo adentro, y sin config.ini se pierde el grupo autorizado de SEDAPAL).
echo ========================================================
pause
