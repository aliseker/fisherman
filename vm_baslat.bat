@echo off
REM ========================================================
REM Sanal makine icin baslatici. OCR ana bilgisayardaki GPU'da yapilir.
REM Asagidaki IP'yi ana bilgisayarin VMware IP'siyle degistir.
REM (Ana bilgisayarda: ipconfig -> "VMware Network Adapter VMnet8" IPv4)
REM ========================================================
cd /d "%~dp0"
set FISHER_OCR_URL=http://192.168.0.1:8765

echo OCR servisi: %FISHER_OCR_URL%
call venv_vm\Scripts\activate.bat
python panel.py
pause
