@echo off
echo ATENCAO: este modo roda em janela de console - fecha se a janela for fechada.
echo Para producao (nao deve ser finalizado por engano), use instalar_servico.bat
echo.
echo Iniciando Agent Gerencial (sync inteligente automatico)...
echo - Primeira execucao: carrega 180 dias
echo - Proximas execucoes: apenas dados novos (incremental)
echo Para encerrar pressione CTRL+C
echo.
python agent_gerencial.py --loop
pause
