@echo off
echo ========================================================
echo Metin2 Fisher - OCR Servisi (RTX 4050)
echo Sanal makineler bu servise baglanir. Pencereyi KAPATMA.
echo ========================================================
echo.
call venv_gpu\Scripts\activate.bat
python ocr_server.py --host 0.0.0.0 --port 8765
pause
