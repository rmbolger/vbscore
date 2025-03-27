"""Volleyball Scoreboard App"""

import json
import uuid
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    Request, HTTPException, status
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

matches = {}  # Stores match data
connections = {}  # Stores active WebSocket connections
match_creation_tracker = {}  # Tracks how many matches each IP creates
MATCH_EXPIRY_TIME = 3 * 60 * 60  # 3 hours in seconds
RATE_LIMIT = 20  # Max matches per IP per hour

async def cleanup_matches():
    """Removes matches that haven't been updated in the last 3 hours."""
    while True:
        current_time = time.time()
        to_delete = [
            match_id for match_id, match in matches.items()
            if (current_time - match["last_updated"]) > MATCH_EXPIRY_TIME
        ]
        for match_id in to_delete:
            del matches[match_id]
            del connections[match_id]
            logging.info("Match %s expired and was removed.", match_id)
        await asyncio.sleep(600)  # Run cleanup every 10 minutes

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan event handler to manage startup and shutdown tasks."""
    task = asyncio.create_task(cleanup_matches())  # Start cleanup task
    yield  # App runs here
    task.cancel()  # Cleanup on shutdown

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_home():
    """Serve the match creation page."""
    return FileResponse("static/index.html")

@app.post("/create_match")
async def create_match(request: Request):
    """Creates a new match and returns admin & viewer links."""
    client_ip = request.client.host

    # Rate limit enforcement
    current_time = time.time()
    match_creation_tracker.setdefault(client_ip, [])
    match_creation_tracker[client_ip] = [
        t for t in match_creation_tracker[client_ip] if (current_time - t) < 3600
    ]

    if len(match_creation_tracker[client_ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")

    match_creation_tracker[client_ip].append(current_time)

    form = await request.form()
    a_name = form.get("a_name", "Team A")
    b_name = form.get("b_name", "Team B")
    a_color = form.get("a_color", "red")
    b_color = form.get("b_color", "blue")

    match_id = str(uuid.uuid4())[:8]
    admin_token = str(uuid.uuid4())[:8]

    matches[match_id] = {
        "score": {"teamA": 0, "teamB": 0},
        "a_name": a_name,
        "b_name": b_name,
        "a_color": a_color,
        "b_color": b_color,
        "admin_token": admin_token,
        "last_updated": time.time()
    }
    connections[match_id] = []

    base_url = f"{request.url.scheme}://{request.url.netloc}"
    admin_link = f"{base_url}/scoreboard/{match_id}?token={admin_token}"
    viewer_link = f"{base_url}/scoreboard/{match_id}"

    logging.info("Match %s created by %s", match_id, client_ip)

    return {"admin_link": admin_link, "viewer_link": viewer_link}

@app.get("/scoreboard/{match_id}")
async def serve_scoreboard(match_id: str):
    """Serve the scoreboard page."""
    if match_id not in matches:
        logging.warning("Attempt to access non-existent match %s", match_id)
        return RedirectResponse("/")
    return FileResponse("static/scoreboard.html")

@app.websocket("/ws/{match_id}")
async def websocket_endpoint(match_id: str, websocket: WebSocket, token: str = None):
    """Handles WebSocket connections for live score updates."""

    # If match does not exist, explicitly close with 1008 before returning
    if match_id not in matches:
        logging.warning("WebSocket attempt for non-existent match %s", match_id)
        await websocket.accept()  # Must accept before closing with a code
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()  # Accepting connection only after validation

    is_admin = token == matches[match_id]["admin_token"]
    connections[match_id].append(websocket)

    logging.info("WebSocket connection opened for match %s (Admin: %s)", match_id, is_admin)

    try:
        await websocket.send_json(matches[match_id])

        while True:
            data = await websocket.receive_text()

            if match_id not in matches:
                logging.warning("Match %s was deleted while WebSocket was active", match_id)
                await websocket.send_json({"error": "Match expired"})
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if is_admin:
                update = json.loads(data)

                if update["action"] == "increment":
                    matches[match_id]["score"][update["team"]] += 1
                elif update["action"] == "decrement":
                    if matches[match_id]["score"][update["team"]] > 0:
                        matches[match_id]["score"][update["team"]] -= 1
                elif update["action"] == "reset":
                    matches[match_id]["score"] = {"teamA": 0, "teamB": 0}

                matches[match_id]["last_updated"] = time.time()

                for conn in connections[match_id]:
                    await conn.send_json(matches[match_id])

    except WebSocketDisconnect:
        logging.info("WebSocket connection closed for match %s", match_id)
    finally:
        if match_id in connections and websocket in connections[match_id]:
            connections[match_id].remove(websocket)
