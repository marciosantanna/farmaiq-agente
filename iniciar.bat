@echo off
echo Iniciando Agent Gerencial (loop automatico a cada 15 min)...
echo Para encerrar pressione CTRL+C
echo.
python agent_gerencial.py --loop --dias 30
pause
