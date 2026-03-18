@echo off
echo Iniciando Agent Gerencial (sync inteligente automatico)...
echo - Primeira execucao: carrega 180 dias
echo - Proximas execucoes: apenas dados novos (incremental)
echo Para encerrar pressione CTRL+C
echo.
python agent_gerencial.py --loop
pause
