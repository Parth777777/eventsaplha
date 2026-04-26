#!/bin/bash
# EventAlpha Multi-Source Scraper - Linux/Mac Script
# This script handles all installation and execution

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║        EventAlpha Hybrid Multi-Source Scraper             ║"
echo "║        Real-time RSS + APIs + NLP Pipeline                ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Check Python installation
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python 3.8+"
    exit 1
fi

echo "✅ Python found"
python3 --version

# Install dependencies
echo ""
echo "📦 Installing dependencies..."
pip3 install -q -r requirements.txt
if [ $? -ne 0 ]; then
    echo "❌ Failed to install dependencies"
    exit 1
fi
echo "✅ Dependencies installed"

# Run scraper
echo ""
echo "🚀 Starting scraper pipeline..."
echo ""
python3 hybrid_scraper.py

echo ""
echo ""
echo "✨ Scraper execution complete!"
echo "📁 Check output: ../data/market_data.json"
echo "📋 Check logs: ./logs/scraper.log"
echo ""
