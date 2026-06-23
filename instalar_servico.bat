@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  Instalar Agent Gerencial como Servico Windows
echo ============================================
echo.
echo IMPORTANTE: execute este arquivo como Administrador
echo (botao direito - Executar como administrador).
echo.
echo Requer o NSSM (Non-Sucking Service Manager):
echo   1. Baixe em https://nssm.cc/download (pacote win64)
echo   2. Extraia nssm.exe e coloque NESTA PASTA (a mesma do agent_gerencial.py)
echo   3. Rode este script novamente
echo.

set PASTA=%~dp0

if not exist "%PASTA%nssm.exe" (
    echo ERRO: nssm.exe nao encontrado em %PASTA%
    pause
    exit /b 1
)

set SERVICO=GerencialAgent

rem Resolve o caminho REAL do python.exe (evita o stub da Microsoft Store
rem em WindowsApps, que nao funciona quando o servico roda como SYSTEM)
for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set PYTHON_REAL=%%P
if "%PYTHON_REAL%"=="" (
    echo ERRO: nao foi possivel localizar o Python instalado.
    pause
    exit /b 1
)

for %%F in ("%PYTHON_REAL%") do set PYDIR=%%~dpF
set PYTHONW=%PYDIR%pythonw.exe
if not exist "%PYTHONW%" set PYTHONW=%PYTHON_REAL%

echo Python real encontrado: %PYTHONW%

rem Se o servico ja existe (ex: reinstalacao apos correcao), remove antes
nssm status %SERVICO% >nul 2>nul
if not errorlevel 1 (
    echo Servico existente encontrado - removendo para reinstalar...
    nssm stop %SERVICO% >nul 2>nul
    nssm remove %SERVICO% confirm >nul 2>nul
)

nssm install %SERVICO% "%PYTHONW%" "%PASTA%agent_gerencial.py --loop"
nssm set %SERVICO% AppDirectory "%PASTA:~0,-1%"
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
