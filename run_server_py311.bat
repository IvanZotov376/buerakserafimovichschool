@echo off
chcp 65001 >nul
echo Проверка Python 3.11...
py -3.11 --version
if errorlevel 1 (
    echo Python 3.11 не найден. Установите Python 3.11 и повторите запуск.
    pause
    exit /b 1
)

echo.
echo Установка/проверка зависимостей...
py -3.11 -m pip install flask flask-cors requests netschoolapi nest-asyncio httpcore

echo.
echo Запуск server.py...
py -3.11 server.py
pause
