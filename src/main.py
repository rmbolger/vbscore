"""Volleyball Scoreboard App"""

import base64
import json
import html
import uuid
import time
import asyncio
import logging
import sys
import os
import zlib
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    Request, HTTPException, status
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Define log format
#LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MATCHES_FILE = Path(os.getenv("MATCHES_FILE", "/opt/vbscore-data/matches.json"))

# Create a handler with the custom format
formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)

# Configure the root logger
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[handler]
)

# Apply the same format to all relevant Uvicorn loggers
for logger_name in ["uvicorn", "uvicorn.access", "websockets.protocol"]:
    logger = logging.getLogger(logger_name)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)

matches = {}  # Stores match data
sessions = {}  # Stores active WebSocket connections
match_creation_tracker = {}  # Tracks how many matches each IP creates
MATCH_EXPIRY_TIME = 3 * 60 * 60  # 3 hours in seconds
RATE_LIMIT = 20  # Max matches per IP per hour
templates = Jinja2Templates(directory="templates")

async def save_matches():
    """Save matches to disk."""
    try:
        logging.info("Saving %s matches to disk.", len(matches))
        with MATCHES_FILE.open("w", encoding="utf-8") as f:
            json.dump(matches, f, ensure_ascii=False, indent=4)
    except Exception as e:  # pylint: disable=broad-except
        logging.warning("Error saving matches: %s", e)


async def load_matches():
    """Load matches from disk."""
    if MATCHES_FILE.exists():
        try:
            with MATCHES_FILE.open("r", encoding="utf-8") as f:
                matches.update(json.load(f))
            for match_id,_ in matches.items():
                sessions[match_id] = []
            logging.info("%s matches loaded from disk.", len(matches))
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Error loading matches: %s", e)
    else:
        logging.info("No existing matches file found at %s.", MATCHES_FILE)


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
            del sessions[match_id]
            logging.info("Match %s expired and was removed.", match_id)
        await asyncio.sleep(600)  # Run cleanup every 10 minutes

def encode_match_state(match):
    """Encodes a match state into a structured JSON object for archiving."""
    match_state = {
        "v": 1,  # schema version
        "d": int(datetime.now().timestamp()),  # epoch timestamp
        "l": match["mLoc"],
        "a": {
            "n": match["a_name"],
            "b": match["a_color_bg"],
            "f": match["a_color_fg"],
            "w": 0,  # Wins (to be calculated)
            "s": []  # Scores (to be added)
        },
        "b": {
            "n": match["b_name"],
            "b": match["b_color_bg"],
            "f": match["b_color_fg"],
            "w": 0,  # Wins (to be calculated)
            "s": []  # Scores (to be added)
        }
    }

    # Process completed set scores
    for set_score in match["sets"]:
        match_state["a"]["s"].append(set_score["teamA"])
        match_state["b"]["s"].append(set_score["teamB"])

    # Calculate wins per team
    for a, b in zip(match_state["a"]["s"], match_state["b"]["s"]):
        if a > b:
            match_state["a"]["w"] += 1
        elif b > a:
            match_state["b"]["w"] += 1

    # Convert to JSON string
    json_str = json.dumps(match_state)
    logging.debug(json_str)

    # Compress and Encode as Base64Url (without padding)
    compressed = zlib.compress(json_str.encode())
    encoded_state = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
    return encoded_state

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan event handler to manage startup and shutdown tasks."""
    await load_matches()  # Load matches at startup
    task = asyncio.create_task(cleanup_matches())  # Start cleanup task
    yield  # App runs here
    await save_matches()  # Save matches on shutdown
    task.cancel()  # Cleanup on shutdown

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_home():
    """Serve the match creation page."""
    return FileResponse("static/index.html")

@app.get("/favicon.ico")
async def serve_favicon():
    """Serve the site favicon."""
    return FileResponse("static/icons/favicon.ico")

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
    a_name = html.escape(form.get("a_name", "Team A")[:20])
    b_name = html.escape(form.get("b_name", "Team B")[:20])
    a_color_bg = form.get("a_color", "#FF0000") # red default
    b_color_bg = form.get("b_color", "#0000FF") # blue default
    a_color_fg = get_contrast_color(a_color_bg)
    b_color_fg = get_contrast_color(b_color_bg)
    match_loc = html.escape(form.get("mLoc", "")[:20])

    # generate a new match ID while avoiding duplicates
    match_id = None
    while not match_id or match_id in matches:
        match_id = str(uuid.uuid4())[:8]

    # This could probably be more secure. But matches only existing for a few
    # hours combined with the relative non-importance of the data it's protecting
    # mean it's probably not worth using something longer and cryptographically strong.
    admin_token = str(uuid.uuid4())[:8]

    matches[match_id] = {
        "sets": [],
        "current_set": 1,
        "score": {"teamA": 0, "teamB": 0},
        "a_name": a_name,
        "b_name": b_name,
        "a_color_bg": a_color_bg,
        "b_color_bg": b_color_bg,
        "a_color_fg": a_color_fg,
        "b_color_fg": b_color_fg,
        "mLoc": match_loc,
        "admin_token": admin_token,
        "last_updated": time.time(),
        "ended": False,
        "viewers": 0
    }
    sessions[match_id] = []

    base_url = f"{request.url.scheme}://{request.url.netloc}"
    admin_link = f"{base_url}/scoreboard/{match_id}?token={admin_token}"
    viewer_link = f"{base_url}/scoreboard/{match_id}"

    logging.info("%s Created match %s", client_ip, match_id)

    return {"admin_link": admin_link, "viewer_link": viewer_link}


@app.get("/scoreboard/{match_id}")
async def serve_scoreboard(request: Request, match_id: str):
    """Serve the scoreboard page."""
    if match_id not in matches:
        logging.warning("Match %s does not exist. Redirecting.", match_id)
        return RedirectResponse("/")

    if matches[match_id]["ended"]:
        logging.info("Redirecting to match archive page for match %s", match_id)
        encoded_state = encode_match_state(matches[match_id])
        archive_url = f"/archive?state={encoded_state}"
        return RedirectResponse(archive_url)

    return templates.TemplateResponse(
        name = "scoreboard.html",
        context = dict(matches[match_id], request=request)
    )


@app.get("/archive")
async def serve_archive():
    """Serve the archive page."""
    return FileResponse("static/archive.html")


@app.websocket("/ws/{match_id}")
async def websocket_endpoint(match_id: str, websocket: WebSocket, token: str = None):
    """Handles WebSocket connections for live score updates."""

    await websocket.accept()

    # Handle invalid or ended matches early
    if await handle_invalid_match(match_id, websocket):
        return
    if await handle_ended_match(match_id, websocket):
        return

    # Track session
    session_id = str(uuid.uuid4())[:8]
    is_admin = token == matches[match_id]["admin_token"]
    add_session(match_id, session_id, websocket)

    try:
        await websocket.send_json(matches[match_id])

        while True:
            data = await websocket.receive_text()
            if await handle_invalid_match(match_id, websocket):
                return

            if not is_admin:
                continue  # Ignore non-admin input

            update = json.loads(data)
            if await process_admin_action(match_id, session_id, update):
                return  # Match ended, connections closed

            # Broadcast updated state
            if not matches[match_id]["ended"]:
                await broadcast_state(match_id)

    except WebSocketDisconnect:
        pass
    finally:
        await remove_session(match_id, session_id, websocket)


async def handle_invalid_match(match_id, websocket):
    """Close WebSocket if the match doesn't exist."""
    if match_id not in matches:
        client_ip,_ = websocket.client
        logging.warning("%s attempted connection to non-existent match %s", client_ip, match_id)
        await websocket.send_json({"redirect": "/"})
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return True
    return False


async def handle_ended_match(match_id, websocket):
    """Redirect client if the match has ended."""
    if matches[match_id]["ended"]:
        archive_url = f"/archive?state={encode_match_state(matches[match_id])}"
        await websocket.send_json({"redirect": archive_url})
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        return True
    return False


def add_session(match_id, session_id, websocket):
    """Add a new session to the tracking list."""
    if match_id in sessions:
        sessions[match_id].append({"session_id": session_id, "websocket": websocket})
        logging.info("%s:%s WebSocket connection opened", match_id, session_id)
    if match_id in matches:
        matches[match_id]["viewers"] = len(sessions[match_id])

async def remove_session(match_id, session_id, websocket):
    """Remove a WebSocket session from tracking."""
    if match_id in sessions:
        logging.info("%s:%s WebSocket connection closing", match_id, session_id)
        sessions[match_id] = [s for s in sessions[match_id] if s["websocket"] != websocket]
    if match_id in matches:
        matches[match_id]["viewers"] = len(sessions[match_id])
    try:
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
    except Exception:  # pylint: disable=broad-except
        pass  # Ignore if already closed


async def process_admin_action(match_id, session_id, update):
    """Process admin actions and return True if the match ended."""
    match = matches[match_id]

    if update["action"] == "increment":
        if match["score"][update["team"]] < 99: # don't go 3-digit
            match["score"][update["team"]] += 1

    elif update["action"] == "decrement":
        if match["score"][update["team"]] > 0:  # don't go negative
            match["score"][update["team"]] -= 1

    elif update["action"] == "reset":
        match["score"] = {"teamA": 0, "teamB": 0}

    elif update["action"] in ("new_set", "end_match"):
        match["sets"].append(match["score"].copy())
        match["score"] = {"teamA": 0, "teamB": 0}
        match["ended"] = update["action"] == "end_match" or match["current_set"] >= 5

        if not match["ended"]:
            match["current_set"] += 1
        else:
            archive_url = f"/archive?state={encode_match_state(match)}"
            await broadcast_redirect(match_id, archive_url)
            return True

    else:
        logging.warning("%s:%s Unrecognized action sent: %s",
                        match_id, session_id, update["action"])

    return False


async def broadcast_state(match_id):
    """Send the updated game state to all connected clients."""
    match_data = matches[match_id]
    for session in sessions[match_id]:
        await session["websocket"].send_json(match_data)


async def broadcast_redirect(match_id, url):
    """Send a redirect message to all clients and close their WebSockets."""
    logging.info("%s Sending redirect to all clients for this match.", match_id)
    for session in sessions[match_id]:
        try:
            await session["websocket"].send_json({"redirect": url})
            await session["websocket"].close(code=status.WS_1000_NORMAL_CLOSURE)
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("%s:%s Error closing WebSocket: %s", match_id, session["session_id"], e)

def relative_luminance(r, g, b):
    """Compute relative luminance as per WCAG 2.1."""
    def adjust(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = adjust(r), adjust(g), adjust(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def get_contrast_color(hex_color: str) -> str:
    """Returns black (#000000) or white (#FFFFFF) based on WCAG contrast ratio."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    # Calculate contrast ratio against white and black
    lum_bg = relative_luminance(r, g, b)
    lum_white = relative_luminance(255, 255, 255)
    lum_black = relative_luminance(0, 0, 0)

    contrast_white = (lum_white + 0.05) / (lum_bg + 0.05)
    contrast_black = (lum_bg + 0.05) / (lum_black + 0.05)

    # Choose the text color that provides better contrast
    return "#FFFFFF" if contrast_white > contrast_black else "#000000"
