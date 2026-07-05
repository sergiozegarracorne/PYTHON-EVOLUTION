@echo off
REM Compila cliente_alertas.py como un .exe portable (todo en un solo
REM archivo). Esta es la version que va en las PCs de la red local que
REM SOLO tienen que ver la alerta a pantalla completa (sin VPN, sin
REM Evolution). El resultado queda en dist\ClienteAlertas.exe

python -m PyInstaller --onefile --name ClienteAlertas --console ^
  --hidden-import=pyttsx3.drivers --hidden-import=pyttsx3.drivers.sapi5 ^
  --hidden-import=pystray._win32 ^
  --hidden-import=PIL._tkinter_finder ^
  cliente_alertas.py

echo.
echo ========================================================
echo Listo. El ejecutable quedo en dist\ClienteAlertas.exe
echo Llevalo a cada PC de la red local (no necesitan VPN). La
echo primera vez que corra se crea config_cliente.ini junto al
echo .exe: edita ahi la IP:puerto del backend si hace falta
echo (por defecto http://1.2.1.42:8500).
echo ========================================================
pause
