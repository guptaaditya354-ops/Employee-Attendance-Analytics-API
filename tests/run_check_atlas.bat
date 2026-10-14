@echo off
cd /d "%~dp0.."
echo Ye script Atlas (ya kisi bhi MongoDB) par chalti hai - .env mein MONGO_URI hona chahiye.
if not exist .env (echo .env file nahi mili. .env.example ki copy banao aur MONGO_URI daalo. & pause & exit /b 1)
echo [1/4] Packages install ho rahe hain...
python -m pip install -q -r requirements.txt httpx
echo [2/4] Server start ho raha hai...
start "api-server" /min cmd /c python -m uvicorn app.main:app --port 8000
echo [3/4] 15 second ruk rahe hain...
timeout /t 15 /nobreak >nul
echo [4/4] Checks chal rahe hain (ye MONGO_DB ke collections ka data delete karke test data daalta hai)...
set MONGO_DB=attendance_db
python tests\smoke_live.py
echo.
echo Upar aakhir mein "FAILURES: 0" dikhe to sab sahi hai.
taskkill /fi "WINDOWTITLE eq api-server*" /t /f >nul 2>&1
pause
