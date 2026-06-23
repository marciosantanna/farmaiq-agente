@echo off
echo Execute como Administrador.
echo.
set SERVICO=GerencialAgent
nssm stop %SERVICO%
nssm remove %SERVICO% confirm
echo.
echo Servico removido. Para reinstalar, rode instalar_servico.bat
pause
