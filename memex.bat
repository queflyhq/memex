@echo off
REM Wrapper used by Claude Code hooks and the MCP stdio entry.
REM Sets PYTHONPATH so 'python -m memex' picks up the live source tree
REM instead of the older memex.exe installed in the venv.
REM Also injects MEMEX_AUTH_TOKEN from daemon.token so the MCP client
REM authenticates against the daemon. Without this, Settings.auth_token
REM stays None (it only reads from env, never from disk) and every
REM write request returns 401.
setlocal
set PYTHONPATH=C:\Users\Admin\memex\src
set DAEMON_TOKEN_FILE=%LOCALAPPDATA%\Quefly\memex\daemon.token
if exist "%DAEMON_TOKEN_FILE%" (
    for /f "usebackq delims=" %%T in ("%DAEMON_TOKEN_FILE%") do set MEMEX_AUTH_TOKEN=%%T
)
"C:\Users\Admin\memex\.venv\Scripts\python.exe" -m memex %*
exit /b %ERRORLEVEL%
