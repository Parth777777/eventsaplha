# EventAlpha - Modular Web Application

## 📁 Project Structure

```
app/
├── index.html                    # Home page
├── explore.html                  # Market Explorer
├── map.html                       # Geo Intelligence Map
├── alerts.html                    # Alerts Center
├── watchlist.html                 # Watchlist Mission Control
├── events.html                    # Events Timeline
└── shared/
    ├── css/
    │   └── style.css              # Consolidated design system
    └── js/
        └── app.js                 # Router, API utilities, helpers
```

## 🚀 Getting Started

### Option 1: Open Locally
1. Open any HTML file in your browser (no build step needed!)
2. All pages share the same navigation and styling
3. Mock data loads automatically for development

### Option 2: Use a Local Server (Recommended)
```bash
# Python 3
python -m http.server 8000

# Node.js
npx http-server

# Any other local server
```
Then visit: `http://localhost:8000`

---

## 🎨 Design System

### Colors (CSS Variables in `style.css`)
- **Primary**: `#adc6ff` (Electric Blue)
- **Secondary**: `#4edea3` (Emerald Green) 
- **Tertiary**: `#ffb2b7` (Rose Red)
- **Background**: `#10141a` (Deep Dark)
- **Surface Layers**: Multiple container levels for depth

### Typography
- **Headlines**: Space Grotesk (bold, geometric)
- **Body**: Inter (clean readability)
- **Data/Numbers**: JetBrains Mono (tabular)

### Components
- `.card` - Base card container
- `.btn` - Button with variants (primary, secondary, tertiary)
- `.badge` - Tag/badge element
- `.progress-bar` - Sentiment/confidence visualizer
- `.sidebar` - Navigation sidebar

---

## 🔌 API Integration Points

All API calls go through the `API` class in `shared/js/app.js`:

```javascript
// Fetch data
const events = await API.get('/events');

// Create record
const newAlert = await API.post('/alerts', { ticker: 'INFY', type: 'bullish' });

// Update
await API.put('/signals/1', { confidence: 0.85 });

// Delete
await API.delete('/alerts/1');
```

**Backend endpoints expected:**
- `GET /api/events` - Fetch events
- `GET /api/signals` - Get ranked signals
- `GET /api/watchlist` - Get user watchlist
- `GET /api/alerts` - Fetch alerts
- `POST /api/alerts` - Create alert
- etc.

---

## 📊 Mock Data

During development, all pages use mock data from `MockData` class:

```javascript
MockData.getLatestEvents()     // Returns past 3 events
MockData.getTopSignals()       // Returns top 3 signals
MockData.getWatchlist()        // Returns sample positions
MockData.getAlerts()           // Returns notifications
MockData.getGeoEvents()        // Returns geo-tagged events
```

Toggle between mock and real data:
```javascript
// In any page's script section
const events = (USE_MOCK_DATA) ? MockData.getLatestEvents() : await API.get('/events');
```

---

## 🧭 Router

The built-in router handles page navigation:

```javascript
// Navigate to a page
router.navigate('home');       // Go to home
router.navigate('explore');    // Go to explore
router.getCurrentPage();       // Returns current page

// Listen to page changes
window.addEventListener('pagechange', (e) => {
    console.log(`Switched to: ${e.detail.page}`);
});
```

**Navigation links** (use `data-nav` attribute):
```html
<a class="sidebar-link" data-nav="explore">Explore</a>
```

---

## 🎯 UI Helper Functions

Reusable formatting utilities in `UIHelper` class:

```javascript
UIHelper.formatNumber(123.456)           // → "123.46"
UIHelper.formatPercent(0.082)            // → "+8.20%"
UIHelper.formatTime(isoString)           // → "2h ago"
UIHelper.getSentimentClass(sentiment)    // → "sentiment-positive"
UIHelper.getConfidenceColor(0.85)        // → "#4edea3" (green)

// Create components
UIHelper.createEventCard(event)          // Returns HTML string
UIHelper.createSignalRow(signal)         // Returns table row HTML
```

---

## 💾 Local Storage

Persist user preferences and data:

```javascript
Storage.set('watchlist', myWatchlist);
const watchlist = Storage.get('watchlist');
Storage.remove('watchlist');
Storage.clear();
```

---

## 🔄 Page Lifecycle

Each page follows this pattern:

```html
<!-- 1. Header & Sidebar (same for all pages) -->
<aside class="sidebar">...</aside>
<header class="main-header">...</header>

<!-- 2. Unique content -->
<main class="main-content">...</main>

<!-- 3. Load libraries & initialize -->
<script src="./shared/js/app.js"></script>
<script>
    document.addEventListener('DOMContentLoaded', function() {
        // Load data after DOM is ready
        const data = MockData.getLatestEvents();
        // Render to page
    });
</script>
```

---

## 🔗 Connecting to Backend

When backend is ready:

1. **Update API baseURL** in `shared/js/app.js`:
```javascript
class API {
  static baseURL = 'http://your-backend-url:8000/api';
}
```

2. **Replace MockData calls** with API calls:
```javascript
// Before
const events = MockData.getLatestEvents();

// After
const events = await API.get('/events');
```

3. **Enable real-time updates** (WebSocket optional):
```javascript
// Listen to page changes
window.addEventListener('pagechange', async (e) => {
    if (e.detail.page === 'alerts') {
        const alerts = await API.get('/alerts');
        renderAlerts(alerts);
    }
});
```

---

## 📱 Responsive Design

All pages are mobile-first responsive:
- **Mobile**: Single column, hidden sidebar (toggle with menu icon)
- **Tablet**: 2-column layout, visible sidebar
- **Desktop**: Full layout, sticky header

---

## 🚀 Next Steps

1. **Backend Setup**: Build FastAPI server, connect `/api/*` endpoints
2. **Real Data**: Replace MockData with actual API calls
3. **Map Integration**: Add Mapbox GL for geo visualization
4. **Advanced Features**:
   - User authentication (login/signup)
   - Websocket for real-time updates
   - Chart libraries (Chart.js, ECharts)
   - Export/reporting functionality

---

## 📝 CSS Customization

Edit `shared/css/style.css` to:
- Change colors (update CSS variables)
- Add new component classes
- Modify animations
- Adjust spacing/sizing

All pages automatically pick up the changes!

---

## ✨ Features Ready for Backend Integration

- ✅ Event feed UI
- ✅ Signal ranking table
- ✅ Watchlist management layout
- ✅ Alert notifications UI
- ✅ Geo event visualization structure
- ✅ User profile panel
- ✅ Responsive navigation
- ✅ Filter interface
- ✅ Real-time sentiment indicators
- ✅ Confidence score visualizers

---

**Status**: MVP Frontend Complete ✨ Ready for Backend Integration 🚀
