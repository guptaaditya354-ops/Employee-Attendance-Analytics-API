@echo off
cd /d "%~dp0.."
echo [1/5] MongoDB start ho raha hai (Docker Desktop chalu hona chahiye)...
docker start mongo >nul 2>&1 || docker run -d --name mongo -p 27017:27017 mongo:7
echo [2/5] Packages install ho rahe hain...
python -m pip install -q -r requirements.txt httpx
echo [3/5] Server start ho raha hai...
start "api-server" /min cmd /c python -m uvicorn app.main:app --port 8000
echo [4/5] 15 second ruk rahe hain...
timeout /t 15 /nobreak >nul
echo [5/5] Checks chal rahe hain...
set MONGO_DB=attendance_db
python tests\smoke_live.py
echo.
echo Upar aakhir mein "FAILURES: 0" dikhe to sab sahi hai.
taskkill /fi "WINDOWTITLE eq api-server*" /t /f >nul 2>&1
pause
