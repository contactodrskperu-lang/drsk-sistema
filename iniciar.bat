@echo off
title DRSK PERU — Sistema de Inventario
cd /d "%~dp0"

set PY=C:\Users\DRSK PERU\AppData\Local\Programs\Python\Python312\python.exe

echo.
echo  ================================
echo   DRSK PERU - Sistema Inventario
echo  ================================

if not exist "inventario.db" (
    echo.
    echo  [1/2] Importando Excel a base de datos...
    "%PY%" import_excel.py
    echo.
)

echo  [OK] Abriendo sistema en el navegador...
echo  URL: http://localhost:5000
echo.
start http://localhost:5000
"%PY%" app.py
pause
