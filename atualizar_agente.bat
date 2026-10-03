@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Atualizar Agent Gerencial (git pull + restart)
echo ============================================
echo IMPORTANTE: execute como Administrador

echo.
echo [1/3] Buscando atualizacoes do repositorio...
git -C "%~dp0." pull origin master
if errorlevel 1 (
    echo ERRO: git pull falhou. Verifique conexao e permissoes.
    pause
    exit /b 1
)

echo.
echo [2/3] Reiniciando servico GerencialAgent...
nssm restart GerencialAgent
if errorlevel 1 (
    echo AVISO: restart falhou, tentando stop+start...
    nssm stop GerencialAgent
    timeout /t 3 /nobreak >nul
    nssm start GerencialAgent
)

echo.
echo [3/3] Status do servico:
nssm status GerencialAgent

echo.
echo ============================================
echo Pronto! Agente atualizado e reiniciado.
echo ============================================
pause
