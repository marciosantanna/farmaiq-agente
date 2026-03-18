@echo off
echo Iniciando Agent Gerencial com webhook (trigger remoto via DuckDNS)...
echo Para encerrar pressione CTRL+C
echo.
python agent_gerencial.py --webhook --dias 30
pause
