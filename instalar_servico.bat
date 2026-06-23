@echo off
setlocal
echo ============================================
echo  Instalar Agent Gerencial como Servico Windows
echo ============================================
echo.
echo IMPORTANTE: execute este arquivo como Administrador
echo (botao direito - Executar como administrador).
echo.
echo Requer o NSSM (Non-Sucking Service Manager):
echo   1. Baixe em https://nssm.cc/download (pacote win64)
echo   2. Extraia nssm.exe e coloque NESTA PASTA (agente_local)
echo   3. Rode este script novamente
echo.

if not exist nssm.exe (
    echo ERRO: nssm.exe nao encontrado nesta pasta.
    pause
    exit /b 1
)

set SERVICO=GerencialAgent
set PASTA=%~dp0

for /f "delims=" %%P in ('where pythonw 2^>nul') do set PYTHONW=%%P
if "%PYTHONW%"=="" (
    echo ERRO: pythonw.exe nao encontrado no PATH. Instale o Python e tente novamente.
    pause
    exit /b 1
)

nssm install %SERVICO% "%PYTHONW%" "%PASTA%agent_gerencial.py --loop"
nssm set %SERVICO% AppDirectory "%PASTA%"
nssm set %SERVICO% DisplayName "Agent Gerencial - Sincronizador Farmasoft"
nssm set %SERVICO% Description "Sincroniza dados do Farmasoft para o sistema gerencial na nuvem. NAO FINALIZAR - parar exige administrador."
nssm set %SERVICO% Start SERVICE_AUTO_START
nssm set %SERVICO% AppExit Default Restart
nssm set %SERVICO% AppRestartDelay 5000
nssm set %SERVICO% AppStdout "%PASTA%servico_stdout.log"
nssm set %SERVICO% AppStderr "%PASTA%servico_stderr.log"

echo.
echo Servico instalado. Iniciando...
nssm start %SERVICO%

echo.
echo ============================================
echo Pronto! O agente agora roda como servico do Windows:
echo  - Inicia automaticamente com o PC (sem precisar logar)
echo  - Nao aparece janela/console para fechar
echo  - Reinicia automaticamente se cair
echo  - Para PARAR (requer admin): nssm stop %SERVICO%
echo  - Para REMOVER: desinstalar_servico.bat
echo ============================================
pause
