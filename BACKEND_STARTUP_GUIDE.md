# Tickwave Complete System Startup Guide

## 🎯 What's New

You now have a **fully integrated backend API** that:
- ✅ Serves real data from scraper to frontend
- ✅ Manages trading signals, events, and predictions
- ✅ Handles watchlist and alerts
- ✅ Provides geo-intelligence endpoints
- ✅ Supports CORS for frontend communication

---

## 🚀 Getting Started (Step by Step)

### Step 1: Initialize Database (First Time Only)
```powershell
cd scraper
python database_schema.py
```
This creates `data/tickwave.db` with all schemas including new alerts & watchlist tables.

---

### Step 2: Start Backend API (Terminal 1)
```powershell
cd backend
python api.py
```

✅ Should show: `Running on http://127.0.0.1:5000`

**The API is now ready to serve data!**

---

### Step 3: Run Scraper (Terminal 2)
```powershell
cd scraper
python hybrid_scraper.py
```

This generates: `data/market_data.json` with real signals

---

### Step 4: Start Frontend (Terminal 3)

**Option A: Python Built-in Server**
```powershell
cd app
python -m http.server 8000
```

**Option B: Open Directly**
```
file:///C:/Users/Admin/OneDrive/Desktop/Event-Trad/app/index.html
```

---

### Step 5: Access the Dashboard
Open your browser:
```
http://localhost:8000
```

✅ You should see **real data** from the scraper!

---

## 📊 Live Data Flow

```
┌─────────────┐
│   Scraper   │  → Collects RSS feeds, market data, NLP extraction
└──────┬──────┘
       ↓
┌──────────────────────┐
│ market_data.json     │  → Raw event intelligence
│ tickwave.db        │  → Persistent storage
└──────────┬───────────┘
           ↓
    ┌──────────────┐
    │ Backend API  │     → REST endpoints (Flask)
    │ :5000        │     → CORS enabled
    └──────┬───────┘
           ↓
    ┌──────────────┐
    │   Frontend   │     → Real-time dashboard
    │   :8000      │     → Displays signals, events, map
    └──────────────┘
```

---

## 🔍 What Changed

### Frontend Changes
- **app.js**: API base URL updated
  ```javascript
  // OLD: API.baseURL = 'http://localhost:8000/api'
  // NEW: API.baseURL = 'http://localhost:5000/api'
  ```
- Automatically tries real API, falls back to mock data

### Backend New Endpoints
- `/api/signals` - Get all active signals
- `/api/events` - Get real-time events  
- `/api/predictions/<id>` - Get price predictions
- `/api/geoevents` - Get geo intelligence (for map)
- `/api/alerts` - Manage alerts
- `/api/watchlist` - Track stocks
- `/api/stats` - System statistics

### Database Added
- **alerts table** - Price/signal alerts
- **watchlist table** - User tracked stocks

---

## 🧪 Test the Backend

In a new terminal:

```powershell
# Test health check
curl http://localhost:5000/api/health

# Get active signals
curl http://localhost:5000/api/signals

# Get events
curl http://localhost:5000/api/events

# Get geo events (for map feature)
curl http://localhost:5000/api/geoevents
```

---

## 🗺️ Map Feature Status

The map is now **ready for real data**:
- ✅ Mapbox GL library loaded
- ✅ `/api/geoevents` endpoint provides location data
- ⏭️ Add Mapbox API key to enable visualization

To activate:
1. Get free API key from [mapbox.com](https://mapbox.com)
2. Update [map.html](../app/map.html) (line ~250):
   ```javascript
   mapboxgl.accessToken = 'YOUR_MAPBOX_API_KEY';
   const map = new mapboxgl.Map({
       container: 'map',
       style: 'mapbox://styles/mapbox/dark-v11',
       center: [78.0193, 28.6139],  // India center
       zoom: 3
   });
   ```

---

## 📋 Troubleshooting

### Backend won't start
```powershell
# Check port 5000 is free
netstat -ano | findstr :5000

# Kill process if needed
taskkill /PID <PID> /F
```

### Frontend shows no data
- ✅ Backend running? Check http://localhost:5000/api/health
- ✅ Scraper ran? Check `data/market_data.json` exists
- ✅ Check browser DevTools → Network tab for API errors

### Can't find database
```powershell
# Ensure data directory exists
mkdir data

# Reinitialize database
cd scraper
python database_schema.py
```

### CORS Errors
- Backend has `Flask-CORS` enabled
- If still getting errors, check browser console
- Ensure both backend and frontend are running

---

## 📊 Example: See Real Data

1. **Start backend**: `python backend/api.py`
2. **Run scraper**: `python scraper/hybrid_scraper.py`
3. **Check JSON**: `cat data/market_data.json`
4. **Test API**: `curl http://localhost:5000/api/signals`
5. **Open frontend**: `http://localhost:8000`

---

## 🎯 Next Priorities

### Immediate (This Week)
- [ ] Add Mapbox API key for geo-intelligence
- [ ] Test all endpoints with real data
- [ ] Verify frontend displays real signals

### Short-term (Next Week)
- [ ] Implement basic authentication
- [ ] Add user preferences storage
- [ ] Real-time WebSocket alerts

### Medium-term
- [ ] Deploy to cloud (Heroku/AWS)
- [ ] Production database (PostgreSQL)
- [ ] Mobile app support

---

## 📞 Architecture Summary

| Component | Port | Status |
|-----------|------|--------|
| Frontend | 8000 | ✅ Ready |
| Backend API | 5000 | ✅ Ready |
| Scraper | - | 🔄 Run manually |
| Database | - | ✅ Created |
| Mapbox | - | ⏭️ Needs API key |

---

## 🚀 You're Ready!

Your full event-driven trading intelligence system is now operational. 

**Command Checklist:**
1. `cd scraper && python database_schema.py` ← Initialize DB
2. `cd backend && python api.py` ← Start API
3. `cd scraper && python hybrid_scraper.py` ← Generate signals
4. `cd app && python -m http.server 8000` ← View dashboard
5. Open `http://localhost:8000` ← See live data

Happy trading! 🚀📈

