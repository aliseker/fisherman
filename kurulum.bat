@echo off
echo ========================================================
echo Metin2 Fisher - GPU Hızlandırmalı Kurulum (Python 3.11)
echo ========================================================
echo.

set PYTHON_PATH="C:\Users\alise\AppData\Local\Programs\Python\Python311\python.exe"

if not exist %PYTHON_PATH% (
    echo [HATA] Python 3.11 bulunamadi. Arka planda yuklemesi devam ediyor olabilir.
    echo Lutfen 1-2 dakika bekleyip tekrar calistirin.
    pause
    exit /b 1
)

echo [1/3] Sanal Ortam (Virtual Environment) olusturuluyor...
%PYTHON_PATH% -m venv venv

echo [2/3] Kutuphaneler yukleniyor (GPU versiyonu)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pip uninstall torch torchvision torchaudio -y
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

echo [3/3] Kurulum Tamamlandi!
echo.
echo ========================================================
echo Botu calistirmak icin "baslat.bat" dosyasini kullanabilirsiniz.
echo ========================================================
pause
