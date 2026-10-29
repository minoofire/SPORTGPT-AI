import asyncio
import os
import sqlite3
from datetime import datetime
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx

app = FastAPI(title="Sports Betting AI Engine")

DB_FILE = "bet_history.db"


# --- DATABASE INITIALIZATION ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Table 1: Historical Analyzed Bets
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            home_team TEXT,
            away_team TEXT,
            market_type TEXT,
            odds REAL,
            win_prob REAL,
            ev_pct REAL,
            recommended_stake REAL,
            ai_insight TEXT,
            timestamp TEXT
        )
    """
    )

    # Table 2: Auto-Synced Live Market Fixtures
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS live_fixtures (
            id TEXT PRIMARY KEY,
            sport_key TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            bookmaker_odds REAL,
            estimated_win_prob REAL,
            ev_pct REAL,
            recommended_stake REAL,
            ai_insight TEXT,
            fetched_at TEXT
        )
    """
    )

    conn.commit()
    conn.close()


init_db()

# Serve Frontend Static Files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


# --- DATA MODELS ---
class BetRequest(BaseModel):
    home_team: str
    away_team: str
    market_type: str = "1X2 (Match Winner)"
    bookmaker_odds: float
    model_win_prob: float  # e.g., 0.55 for 55%
    bankroll: float = 100000.0


# --- STRICT DETERMINISTIC MATHEMATICAL ENGINE ---
def calculate_metrics(odds: float, prob: float, bankroll: float = 100000.0):
    """Calculates EV% and Fractional Kelly Criterion Stake Amount."""
    # EV = (Probability * Decimal Odds) - 1.0
    ev = (prob * odds) - 1.0

    # Fractional Kelly Criterion (Quarter Kelly for risk management)
    b = odds - 1.0
    q = 1.0 - prob
    if b > 0:
        kelly_fraction = (b * prob - q) / b
        stake_pct = max(0.0, kelly_fraction * 0.25)
    else:
        stake_pct = 0.0

    stake_amount = bankroll * stake_pct
    return round(ev * 100, 2), round(stake_pct * 100, 2), round(stake_amount, 2)


# --- MANUAL FIXTURE ANALYZER ENDPOINT ---
@app.post("/api/analyze")
async def analyze_fixture(req: BetRequest):
    ev_pct, stake_pct, stake_amount = calculate_metrics(
        req.bookmaker_odds, req.model_win_prob, req.bankroll
    )
    is_value = ev_pct > 0

    # LLM Qualitative Tactical Layer
    api_key = os.getenv("GROQ_API_KEY")
    ai_insight = "Quantitative metrics computed successfully. Provide a GROQ_API_KEY for qualitative tactical commentary."

    if api_key:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an elite quantitative sports betting analyst."
                        " Provide a punchy, 2-sentence tactical breakdown"
                        " assessing this fixture's risk profile."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Fixture: {req.home_team} vs {req.away_team} | Market:"
                        f" {req.market_type} | Odds: {req.bookmaker_odds} |"
                        f" Model Prob: {req.model_win_prob*100:.1f}% | EV:"
                        f" {ev_pct}%"
                    ),
                },
            ],
            "temperature": 0.4,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(url, json=payload, headers=headers)
                if res.status_code == 200:
                    ai_insight = res.json()["choices"][0]["message"]["content"]
            except Exception:
                pass

    # Log to SQLite
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO bets (home_team, away_team, market_type, odds, win_prob, ev_pct, recommended_stake, ai_insight, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            req.home_team,
            req.away_team,
            req.market_type,
            req.bookmaker_odds,
            req.model_win_prob,
            ev_pct,
            stake_amount,
            ai_insight,
            datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "home_team": req.home_team,
        "away_team": req.away_team,
        "market_type": req.market_type,
        "ev_percentage": ev_pct,
        "is_value": is_value,
        "recommended_stake": stake_amount,
        "stake_percentage": stake_pct,
        "ai_insight": ai_insight,
    }


# --- AUTOMATED API ODDS INGESTION ---
async def fetch_and_store_odds_api():
    """Fetches upcoming fixtures and real odds from The Odds API."""
    odds_api_key = os.getenv("ODDS_API_KEY")
    if not odds_api_key:
        print("⚠️ ODDS_API_KEY not found in environment variables. Skipping auto-sync.")
        return

    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?apiKey={odds_api_key}&regions=eu&markets=h2h"

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                print(f"Error fetching odds: status {res.status_code}")
                return

            fixtures = res.json()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()

            for item in fixtures:
                match_id = item.get("id")
                home = item.get("home_team")
                away = item.get("away_team")
                start_time = item.get("commence_time")

                bookmakers = item.get("bookmakers", [])
                if not bookmakers:
                    continue

                h2h_outcomes = bookmakers[0]["markets"][0]["outcomes"]
                home_odds = next(
                    (o["price"] for o in h2h_outcomes if o["name"] == home), None
                )

                if not home_odds:
                    continue

                estimated_prob = round(min(0.85, (1.0 / home_odds) * 1.05), 2)
                ev_pct, stake_pct, stake_amount = calculate_metrics(
                    home_odds, estimated_prob
                )

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO live_fixtures 
                    (id, sport_key, home_team, away_team, commence_time, bookmaker_odds, estimated_win_prob, ev_pct, recommended_stake, ai_insight, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        match_id,
                        "soccer_epl",
                        home,
                        away,
                        start_time,
                        home_odds,
                        estimated_prob,
                        ev_pct,
                        stake_amount,
                        f"Auto-ingested fixture. Calculated {ev_pct}% EV against market pricing.",
                        datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                    ),
                )

            conn.commit()
            conn.close()
            print("✅ Live market fixtures auto-ingested into database.")
        except Exception as e:
            print(f"❌ Auto-ingest exception: {str(e)}")


# --- 12-HOUR PERIODIC BACKGROUND LOOP ---
async def twice_daily_odds_sync():
    """Runs automatically every 12 hours (43,200 seconds)."""
    while True:
        print("🔄 Background Task Running: Syncing live fixtures...")
        await fetch_and_store_odds_api()
        await asyncio.sleep(43200)  # Wait 12 hours


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(twice_daily_odds_sync())


# --- API QUERY ENDPOINTS ---
@app.post("/api/sync-fixtures")
async def trigger_sync(background_tasks: BackgroundTasks):
    background_tasks.add_task(fetch_and_store_odds_api)
    return {"status": "Sync triggered in background."}


@app.get("/api/live-fixtures")
async def get_live_fixtures():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM live_fixtures ORDER BY ev_pct DESC LIMIT 15")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/history")
async def get_history():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bets ORDER BY id DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
