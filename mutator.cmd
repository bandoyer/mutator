@echo off
rem Run the mutation tool on Windows, creating a local virtualenv on first use.
rem The venv comes from py -3, or from python when the py launcher is missing.
rem It counts as made only once it imports mutator, so an install cut short is
rem made again. A failed install removes .venv only when this run made it.
rem UTF-8 mode lets output sent to a file or a pipe hold characters outside the
rem Windows code page.
setlocal
set "PYTHONUTF8=1"
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" goto make
"%PY%" -c "import mutator" 2>nul
if not errorlevel 1 goto run
:make
set "MADE="
if not exist "%ROOT%.venv" set "MADE=1"
where py >nul 2>nul
if errorlevel 1 goto python
>&2 echo Making .venv with py -3
py -3 -m venv "%ROOT%.venv"
goto made
:python
>&2 echo Making .venv with python
python -m venv "%ROOT%.venv"
:made
if errorlevel 1 goto failed
"%PY%" -m pip install -e "%ROOT%."
if errorlevel 1 goto failed
:run
"%PY%" -m mutator %*
exit /b %ERRORLEVEL%
:failed
set "CODE=%ERRORLEVEL%"
if defined MADE rmdir /s /q "%ROOT%.venv" 2>nul
exit /b %CODE%
