@echo off
REM Wrapper used by Claude Code hooks and the MCP stdio entry.
REM Sets PYTHONPATH so 'python -m memex' picks up the live source tree
REM instead of the older memex.exe installed in the venv.
setlocal
set PYTHONPATH=C:\Users\Admin\memex\src
"C:\Users\Admin\memex\.venv\Scripts\python.exe" -m memex %*
exit /b %ERRORLEVEL%
