@echo off
rem Ежедневный разбор дня агента моделью (планировщик Windows, задача
rem EDU-agent-analyst, 12:15 МСК — после Э0 и Э1). Биллинг от подписки
rem Claude Code: внутри python зовёт claude -p. Лог — в output\ (.gitignore).
cd /d "%~dp0.."
if not exist output mkdir output
echo ---- %date% %time% ---->> output\agent-analyst.log
python -m sync.agent_analyst --local >> output\agent-analyst.log 2>&1
