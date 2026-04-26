# EventAlpha Backend API

## 📋 Overview

The EventAlpha Backend API is a Flask-based REST server that:
- Serves trading signals, events, and predictions from the scraper
- Stores and retrieves data from SQLite database
- Provides endpoints for alerts, watchlist, and statistics
- Enables real-time communication between scraper and frontend

---

## 🚀 Quick Start

### Option 1: Windows (Recommended)
```powershell
cd backend
.\run_backend.bat
```

### Option 2: Linux/Mac
```bash
cd backend
chmod +x run_backend.sh
./run_backend.sh
```

### Option 3: Manual (Any OS)
```bash
cd backend
pip install -r requirements.txt
python api.py
```

---

## 📡 API Endpoints

### Signals
- `GET /api/signals` - Get all active signals
- `GET /api/signals/<id>` - Get signal details
- `POST /api/signals` - Create new signal

### Events
- `GET /api/events` - Get all events
- `GET /api/events/<id>` - Get event details

### Predictions
- `GET /api/predictions/<signal_id>` - Get predictions for signal

### Geo Intelligence
- `GET /api/geoevents` - Get geopolitical events and locations

### Alerts
- `GET /api/alerts` - Get user alerts
- `POST /api/alerts` - Create new alert

### Watchlist
- `GET /api/watchlist` - Get watchlist
- `POST /api/watchlist` - Add to watchlist

### Stats
- `GET /api/stats` - Get system statistics

### Health
- `GET /api/health` - Health check

---

## 🔄 System Architecture

```
┌──────────────────┐
│ Flask Backend    │
│ (localhost:5000) │
└────────┬─────────┘
         │
         ├─→ SQLite Database (eventalpha.db)
         ├─→ JSON Data (market_data.json)
         └─→ CORS enabled for frontend
         
┌──────────────────────────────────────┐
│ Frontend (localhost:8000 or file://)  │
│ - index.html, explore.html, etc.      │
│ - app.js calls http://localhost:5000  │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│ Scraper (runs independently)          │
│ - Collects RSS feeds, market data     │
│ - Generates signals                   │
│ - Outputs market_data.json            │
│ - Writes to eventalpha.db             │
└──────────────────────────────────────┘
```

---

## 🗄️ Database Schema

### Tables
- **signals** - Trading signals with alpha scores
- **predictions** - Multi-horizon price predictions
- **backtest_results** - Historical performance records
- **performance_summary** - Aggregated metrics
- **market_regimes** - Daily market regime snapshots
- **alerts** - User price/signal alerts
- **watchlist** - Tracked stocks

---

## 🧪 Running Everything Together

### Terminal 1: Initialize Database
```bash
cd scraper
python database_schema.py
```

### Terminal 2: Start Backend
```bash
cd backend
python api.py
```
Should show: `Running on http://0.0.0.0:5000`

### Terminal 3: Run Scraper
```bash
cd scraper
python hybrid_scraper.py
```
Generates: `../data/market_data.json`

### Terminal 4: Start Frontend
```bash
# Option A: Python
cd app
python -m http.server 8000

# Option B: Node.js
npx http-server app -p 8000

# Option C: Just open in browser
# file:///path/to/Event-Trad/app/index.html
```

Then open: **http://localhost:8000**

---

## 📊 Data Flow

1. **Scraper generates signals** → writes to `data/market_data.json`
2. **Backend loads JSON** → exposes via REST API
3. **Backend queries database** → if available, returns persistent data
4. **Frontend calls API** → receives signals, events, predictions
5. **Frontend displays data** → real-time dashboard

---

## 🔧 Configuration

### Backend Settings (api.py)
```python
DATABASE_PATH = '../data/eventalpha.db'
JSON_DATA_PATH = '../data/market_data.json'
API_PORT = 5000
API_HOST = '0.0.0.0'
```

### Frontend Settings (app.js)
```javascript
API.baseURL = 'http://localhost:5000/api'
```

---

## 📝 Example API Calls

### Get active signals
```bash
curl http://localhost:5000/api/signals
```

### Get specific event
```bash
curl http://localhost:5000/api/events/1
```

### Create alert
```bash
curl -X POST http://localhost:5000/api/alerts \
  -H "Content-Type: application/json" \
  -d '{"ticker":"INFY","alert_type":"price","condition":"above","threshold":2900}'
```

### Get geopolitical events (for map)
```bash
curl http://localhost:5000/api/geoevents
```

### Get system stats
```bash
curl http://localhost:5000/api/stats
```

---

## 🐛 Troubleshooting

### Backend won't start
- Ensure Python 3.8+ is installed
- Run: `pip install -r requirements.txt --upgrade`
- Check port 5000 is available: `netstat -an | grep 5000`

### Frontend can't reach API
- Ensure backend is running on port 5000
- Check CORS headers are enabled (Flask-CORS)
- Open browser DevTools → Network tab → check requests

### No data appearing
- Run scraper: `python hybrid_scraper.py` 
- Check `data/market_data.json` exists
- Check database exists: `data/eventalpha.db`

### Port already in use
```bash
# Windows: Kill process using port 5000
netstat -ano | findstr :5000
taskkill /PID <PID> /F

# Linux/Mac:
lsof -i :5000
kill -9 <PID>
```

---

## 📦 Dependencies

- Flask 2.3.3 - Web framework
- Flask-CORS 4.0.0 - Cross-origin requests
- Python 3.8+ - Runtime

---

## 🎯 Next Steps

1. ✅ Backend API running
2. ✅ Database initialized
3. ⏭️ Front Scraper configured and running
4. ⏭️ Frontend dashboard displaying real data
5. ⏭️ Authentication/login (optional)
6. ⏭️ Mapbox integration for geo intelligence

