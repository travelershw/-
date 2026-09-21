@echo off
rem 轻语桌宠：双击启动（pythonw，不弹控制台）
rem 起没起来、崩没崩，都写在 pet_log.txt 里（app 自己写的，别在这里重定向，会打架）
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0pet.py"
