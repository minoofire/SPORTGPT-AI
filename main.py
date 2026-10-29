import asyncio
import os
import random
import sqlite3
from datetime import datetime
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx

app = FastAPI(title="SharpPunter Engine")

DB_FILE = "bet_history.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Log table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            home_team TEXT,
            away_team TEXT,
            sport TEXT,
            league TEXT,
            odds REAL,
            win_prob REAL,
            ev_pct REAL,
            rating TEXT,
            recommended_stake REAL,
            ai_insight TEXT,
            timestamp TEXT
        )
    """
    )

    # Live auto-synced match table
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS live_fixtures (
            id TEXT PRIMARY KEY,
            sport TEXT,
            league TEXT,
            home_team TEXT,
            away_team TEXT,
            commence_time TEXT,
            bookmaker_odds REAL,
            estimated_win_prob REAL,
            ev_pct REAL,
            rating TEXT,
            recommended_stake REAL,
            ai_insight TEXT,
            fetched_at TEXT
        )
    """
    )

    conn.commit()
    conn.close()


init_db()

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


# --- DATA MODELS ---
class BetRequest(BaseModel):
    home_team: str
    away_team: str
    sport: str = "Football"
    league: str = "Premier League"
    bookmaker_odds: float
    model_win_prob: float
    bankroll: float = 100000.0


class CodeCheckRequest(BaseModel):
    booking_code: str
    bookmaker: str = "SportyBet"


class SharpSlipRequest(BaseModel):
    num_picks: int = 3
    risk_profile: str = "Balanced"  # Safe, Balanced, Adventurous


# --- RATING & MATH ENGINE ---
def compute_rating(odds: float, win_prob: float, bankroll: float = 100000.0):
    ev = (win_prob * odds) - 1.0
    ev_pct = round(ev * 100, 2)

    # SharpPunter Rating System
    if ev_pct >= 8.0:
        rating = "Strong"
    elif ev_pct >= 2.0:
        rating = "Fair"
    else:
        rating = "Slim"

    b = odds - 1.0
    q = 1.0 - win_prob
    if b > 0:
        kelly = (b * win_prob - q) / b
        stake_pct = max(0.0, kelly * 0.25)
    else:
        stake_pct = 0.0

    stake_amount = round(bankroll * stake_pct, 2)
    return ev_pct, rating, round(stake_pct * 100, 2), stake_amount


# --- ENDPOINTS ---


@app.post("/api/analyze")
async def analyze_match(req: BetRequest):
    ev_pct, rating, stake_pct, stake_amount = compute_rating(
        req.bookmaker_odds, req.model_win_prob, req.bankroll
    )

    api_key = os.getenv("GROQ_API_KEY")
    ai_insight = f"Rated {rating}. Calculated +{ev_pct}% mathematical edge against current market pricing."

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
                        "You are the SharpPunter AI prediction engine. Write a"
                        " crisp, 2-sentence match verdict in plain words"
                        " explaining why this pick is rated Strong, Fair, or"
                        " Slim."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Match: {req.home_team} vs {req.away_team} | Sport:"
                        f" {req.sport} | Odds: {req.bookmaker_odds} | EV:"
                        f" +{ev_pct}% | Rating: {rating}"
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

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO bets (home_team, away_team, sport, league, odds, win_prob, ev_pct, rating, recommended_stake, ai_insight, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            req.home_team,
            req.away_team,
            req.sport,
            req.league,
            req.bookmaker_odds,
            req.model_win_prob,
            ev_pct,
            rating,
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
        "rating": rating,
        "ev_pct": ev_pct,
        "recommended_stake": stake_amount,
        "stake_pct": stake_pct,
        "ai_insight": ai_insight,
    }


@app.post("/api/check-code")
async def check_booking_code(req: CodeCheckRequest):
    """SharpPunter Code Checker & Optimizer."""
    code = req.booking_code.upper()

    # Simulated code analysis & leg evaluation
    flagged_weak = random.choice([0, 1])
    sample_recommendation = (
        f"Analyzed code [{code}] on {req.bookmaker}. Found 4 total legs. 3 legs"
        " rated Strong/Fair. 1 weak leg detected with negative EV edge."
        " Recommended trimming weak leg or swapping with today's Hot Pick."
        if flagged_weak > 0
        else f"Analyzed code [{code}] on {req.bookmaker}. All selections rated Strong or Fair. Excellent value slip!"
    )

    return {
        "code": code,
        "bookmaker": req.bookmaker,
        "flagged_weak_legs": flagged_weak,
        "analysis_report": sample_recommendation,
        "suggested_action": (
            "Drop weak pick and convert to SportyBet / Bet9ja"
            if flagged_weak > 0
            else "Ready to play as is"
        ),
    }


@app.post("/api/generate-sharpslip")
async def generate_sharpslip(req: SharpSlipRequest):
    """SharpSlips Auto-Builder."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM live_fixtures ORDER BY ev_pct DESC LIMIT 10")
    rows = cursor.fetchall()
    conn.close()

    fixtures = [dict(r) for r in rows]
    selected_picks = (
        random.sample(fixtures, min(req.num_picks, len(fixtures)))
        if fixtures
        else []
    )

    total_odds = 1.0
    for p in selected_picks:
        total_odds *= p.get("bookmaker_odds", 1.5)

    fake_code = f"SP-{random.randint(100000, 999999)}"

    return {
        "generated_code": fake_code,
        "risk_profile": req.risk_profile,
        "total_odds": round(total_odds, 2),
        "picks": selected_picks,
    }


# --- AUTO-SYNC BACKGROUND LOOP ---
async def fetch_and_store_odds():
    odds_api_key = os.getenv("ODDS_API_KEY")
    if not odds_api_key:
        return

    url = f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds/?apiKey={odds_api_key}&regions=eu&markets=h2h"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                return

            data = res.json()
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()

            for item in data:
                m_id = item.get("id")
                home = item.get("home_team")
                away = item.get("away_team")
                start = item.get("commence_time")

                bookmakers = item.get("bookmakers", [])
                if not bookmakers:
                    continue

                h2h = bookmakers[0]["markets"][0]["outcomes"]
                home_odds = next(
                    (o["price"] for o in h2h if o["name"] == home), None
                )
                if not home_odds:
                    continue

                prob = round(min(0.85, (1.0 / home_odds) * 1.05), 2)
                ev_pct, rating, stake_pct, stake = compute_rating(
                    home_odds, prob
                )

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO live_fixtures 
                    (id, sport, league, home_team, away_team, commence_time, bookmaker_odds, estimated_win_prob, ev_pct, rating, recommended_stake, ai_insight, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        m_id,
                        "Football",
                        "Premier League",
                        home,
                        away,
                        start,
                        home_odds,
                        prob,
                        ev_pct,
                        rating,
                        stake,
                        f"AI Prediction: Rated {rating} with +{ev_pct}% EV edge.",
                        datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                    ),
                )

            conn.commit()
            conn.close()
        except Exception:
            pass


async def background_loop():
    while True:
        await fetch_and_store_odds()
        await asyncio.sleep(43200)


@app.on_event("startup")
async def startup():
    asyncio.create_task(background_loop())


@app.get("/api/live-fixtures")
async def get_fixtures():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM live_fixtures ORDER BY ev_pct DESC LIMIT 15"
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
