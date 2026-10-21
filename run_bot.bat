@echo off
setlocal
rem AdReel bot launcher. Double-click, or: run_bot.bat [docker]
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [!] Не найден uv. Поставь его командой в PowerShell:
    echo     powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    goto :end
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [!] Создал .env из .env.example. Впиши TELEGRAM_BOT_TOKEN и ключи и запусти снова.
    echo     Свой OWNER_TELEGRAM_ID узнаешь командой /id в боте.
    start "" notepad ".env"
    goto :end
)

if /i "%~1"=="docker" goto :docker

where ffmpeg >nul 2>nul
if errorlevel 1 echo [!] ffmpeg не найден в PATH: без него не будет монтажа и разбора музыки.

:loop
echo [%date% %time%] Запускаю бота. Остановить: Ctrl+C.
rem uv run сам доставит зависимости, если uv.lock поменялся
uv run python -m adreel.bot
set CODE=%errorlevel%
if "%CODE%"=="0" goto :end
if "%CODE%"=="2" (
    echo [!] Бот не стартовал: не хватает настроек в .env, причина в логе выше.
    goto :end
)
rem Упал на ходу: перезапускаем. Оплаченные генерации не пропадут, их подберёт «Доделать».
echo [!] Бот упал, код %CODE%. Перезапуск через 10 секунд, Ctrl+C отменит.
timeout /t 10 /nobreak >nul
goto :loop

:docker
where docker >nul 2>nul
if errorlevel 1 (
    echo [!] Docker не найден.
    goto :end
)
docker compose up -d --build
if errorlevel 1 goto :end
echo Бот и локальный Bot API подняты. Ниже логи бота: Ctrl+C закроет логи, бот продолжит работать.
docker compose logs -f bot
goto :end

:end
echo.
pause
endlocal
