# 🚀 Tickwave - START HERE

## Your Event-Driven Trading System is Ready!

You just built:
- ✅ **Flask REST API** (Backend)
- ✅ **6-Page Dashboard** (Frontend) 
- ✅ **Real-Time Scraper** (Data pipeline)
- ✅ **AI Signal Engine** (Alpha scoring)
- ✅ **SQLite Database** (Persistent storage)

---

## 🎯 Get Running in 5 Minutes

Open **4 PowerShell/Terminal windows** and run these commands (one per window):

### Window 1: Initialize Database
```powershell
cd scraper
python database_schema.py
# Waits 2 seconds, then closes
```

### Window 2: Start Backend API ★ MUST RUN FIRST ★
```powershell
cd backend
python api.py
# Stays running - you'll see: "Running on http://127.0.0.1:5000"
```

### Window 3: Run Scraper
```powershell
cd scraper
python hybrid_scraper.py
# Collects data and outputs: data/market_data.json
```

### Window 4: Start Frontend
```powershell
cd app
python -m http.server 8000
# Stays running - you'll see: "Serving HTTP on 0.0.0.0:8000"
```

---

## 🌐 View the Dashboard

Once all 4 windows show "Running" or "Serving", open your browser:

### Go to: **http://localhost:8000**

You should see:
- 📊 Real trading signals with alpha scores
- 🗞️ Live events from RSS feeds
- 📈 Price predictions
- 🗺️ Geopolitical map
- ⚠️ Alerts center
- ⭐ Watchlist

---

## ✅ Verification Checklist

- [ ] Window 2 (Backend): Shows `Running on http://127.0.0.1:5000`
- [ ] Window 4 (Frontend): Shows `Serving HTTP on 0.0.0.0:8000`
- [ ] Browser shows dashboard at `http://localhost:8000`
- [ ] Dashboard displays real signals (not just placeholder)
- [ ] No errors in browser console (F12 → Console)

---

## 🧪 Test Each Component

### Test Backend API
```powershell
# In any terminal:
curl http://localhost:5000/api/health
# Should return: {"status":"ok"...}

curl http://localhost:5000/api/signals
# Should return: JSON with signal data
```

### Test Frontend
- Reload page: `F5` or `Ctrl+R`
- Open DevTools: `F12`
- Check Network tab for API calls to `localhost:5000`

### Test Scraper
```powershell
# Check if data was generated:
dir data/market_data.json
```

---

## 📚 Documentation

- **Full Backend Setup**: [backend/README.md](backend/README.md)
- **Complete System Guide**: [BACKEND_STARTUP_GUIDE.md](BACKEND_STARTUP_GUIDE.md)
- **Scraper Configuration**: [SCRAPER_SETUP.md](SCRAPER_SETUP.md)
- **Implementation Details**: [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)

---

## 🔥 Features Now Live

### Trading Signals
- Alpha scores (0-100)
- Confidence levels
- Multi-horizon predictions (1D, 3D, 5D, 20D)
- Price targets

### Real-Time Intelligence
- 15+ RSS feeds monitored
- NLP-based event extraction
- Sentiment analysis
- Market regime detection

### Geo-Intelligence
- *Ready for Mapbox integration*
- Location-based event mapping
- Geopolitical risk scoring

### User Tools
- Watchlist tracking
- Price alerts
- Signal filtering
- Performance metrics

---

## 🆘 Troubleshooting

### Backend won't start
```powershell
# Check if port 5000 is in use:
netstat -ano | findstr :5000

# If blocked, kill it:
taskkill /PID <PID> /F
```

### Frontend shows no data
1. Check backend is running (Window 2)
2. Check scraper ran (look for `data/market_data.json`)
3. Open DevTools (F12) → Console → look for errors

### Can't connect to backend
- Make sure Window 2 shows: `Running on http://127.0.0.1:5000`
- Frontend should auto-retry
- Check firewall isn't blocking port 5000

---

## 🎯 What's Next?

### This Week
- [ ] Get familiar with the dashboard
- [ ] Run scraper on your monitored stocks
- [ ] Test signal accuracy

### Next Week  
- [ ] Add Mapbox API key for map
- [ ] Configure alerts
- [ ] Backtest strategies

### Later
- [ ] Deploy to cloud
- [ ] Add authentication
- [ ] Real-time WebSocket updates

---

## 💡 Pro Tips

1. **Keep windows open**: Backend, Frontend, and Scraper should all be running
2. **Reload often**: Frontend caches data - press F5 to refresh
3. **Check logs**: Each window shows error messages if something goes wrong
4. **Run scraper frequently**: To get fresh market data, re-run Window 3
5. **Test API manually**: Use curl to verify backend is responding

---

## 📞 System Status

| Component | Expected Output | Port |
|-----------|---|---|
| **Database Init** | Completes silently | - |
| **Backend API** | `Running on http://127.0.0.1:5000` | 5000 |
| **Scraper** | `✅ Scraper completed` | - |
| **Frontend** | `Serving HTTP on 0.0.0.0:8000` | 8000 |

---

## 🚀 You're All Set!

**Just run those 4 commands in 4 windows and you're trading!**

Questions? Check [BACKEND_STARTUP_GUIDE.md](BACKEND_STARTUP_GUIDE.md) for detailed instructions.

Happy trading! 📈🚀
