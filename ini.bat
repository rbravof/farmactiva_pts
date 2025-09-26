@echo off
setlocal enabledelayedexpansion

echo 🧠 Verificando procesos que usan el puerto 8002...

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8002') do (
    set PID=%%a
    echo 🔥 Terminando proceso con PID !PID! ...
    taskkill /F /PID !PID! >nul 2>&1
)

echo ✅ Puerto liberado. Iniciando servidor Uvicorn...

cd /d C:\Users\rbrav\FarmactivaPorTuSalud
call env\Scripts\activate.bat
uvicorn app.main:app --reload --port 8002

pause
