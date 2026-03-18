@echo off
echo Iniciando Agent Gerencial com webhook (trigger remoto via DuckDNS)...
echo - Primeira execucao: carrega 180 dias
echo - Proximas execucoes: apenas dados novos (incremental)
echo Para encerrar pressione CTRL+C
echo.
python agent_gerencial.py --webhook
pause
