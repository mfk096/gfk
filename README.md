# CZ DataMatrix PDF App

Упаковка в исполняемый файл через PyInstaller.

## Быстрая сборка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pyinstaller --clean --noconfirm cz_datamatrix_pdf_app.spec
```

После сборки бинарник будет в папке `dist/`:

- Linux/macOS: `dist/cz_datamatrix_pdf_app`
- Windows: `dist/cz_datamatrix_pdf_app.exe`

## Формат печати этикеток

Приложение формирует PDF так, чтобы каждая этикетка была на отдельном листе размером **20×30 мм**.
Текст кода на этикетке автоматически переносится и масштабируется, чтобы полностью помещаться в пределах этикетки.

## Сборка именно `.exe` (Windows)

`PyInstaller` собирает бинарники только под текущую ОС. Поэтому для `.exe` сборку нужно запускать на Windows:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pyinstaller --clean --noconfirm cz_datamatrix_pdf_app.spec
```
