@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Metin2 (Gameforge) yonetici olarak calistigi icin pencere gecisi (focus)
REM ve tus gonderiminin Windows UIPI tarafindan engellenmemesi icin Admin yetkisi alinir.
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Yonetici izni aliniyor...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo ========================================================
echo Metin2 Fisher - RTX 4050 GPU Multi-Client Modu (1-4 Hesap)
echo ========================================================
echo.

call venv_gpu\Scripts\activate.bat
python panel.py
pause
