@echo off
echo Resetando cache de sync (forcara sync completo na proxima execucao)...
if exist sync_state.json (
    del sync_state.json
    echo sync_state.json removido.
) else (
    echo sync_state.json nao encontrado (ja limpo).
)
echo.
echo Agora inicie o agent normalmente com iniciar_webhook.bat
pause
