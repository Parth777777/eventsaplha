@echo off
REM EventAlpha Multi-Source Scraper - Windows Batch Script
REM This script handles all installation and execution

setlocal enabledelayedexpansion

echo.
echo ╔════════════════════════════════════════════════════════════╗
echo ║        EventAlpha Hybrid Multi-Source Scraper             ║
echo ║        Real-time RSS + APIs + NLP Pipeline                ║
echo ╚════════════════════════════════════════════════════════════╝
echo.

REM Check Python installation
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python not found. Please install Python 3.8+
    pause
    exit /b 1
)

echo ✅ Python found
python --version

REM Install dependencies
echo.
echo 📦 Installing dependencies...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ❌ Failed to install dependencies
    pause
    exit /b 1
)
echo ✅ Dependencies installed

REM Run scraper
echo.
echo 🚀 Starting scraper pipeline...
echo.
python hybrid_scraper.py

echo.
echo.
echo ✨ Scraper execution complete!
echo 📁 Check output: ..\data\market_data.json
echo 📋 Check logs: .\logs\scraper.log
echo.
pause
