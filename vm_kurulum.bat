@echo off
chcp 65001 >nul
REM ========================================================
REM Sanal makine KURULUM + GUNCELLEME (tek dosya)
REM Gereken: Git ve Python 3.11 (PATH'te). Chrome onerilir (fiyat cekme icin).
REM Ilk kurulumda da, sonraki guncellemelerde de ayni dosyayi calistir.
REM ========================================================
cd /d "%~dp0"

where git >nul 2>nul || (echo [HATA] Git bulunamadi: https://git-scm.com/download/win & pause & exit /b 1)
where python >nul 2>nul || (echo [HATA] Python bulunamadi: https://www.python.org/downloads/release/python-3119/ & pause & exit /b 1)

echo [1/3] Kod GitHub'dan guncelleniyor...
git pull --ff-only

echo [2/3] Sanal ortam ve kutuphaneler...
if not exist venv_vm python -m venv venv_vm
call venv_vm\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements_vm.txt

echo [3/3] Tamam. Botu baslatmak icin vm_baslat.bat
pause
