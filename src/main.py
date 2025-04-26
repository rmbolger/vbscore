"""Volleyball Scoreboard App"""

import json
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    Request
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from match_manager import MatchManager
from rate_limiter import rate_limit, rate_limiter_cleanup_loop

# Define log format
#LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

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

templates = Jinja2Templates(directory="templates")
mgr = MatchManager()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan event handler to manage startup and shutdown tasks."""
    await mgr.load_matches()  # Load matches at startup

    match_cleanup_task = asyncio.create_task(mgr.cleanup_matches())
    rate_limiter_cleanup_task = asyncio.create_task(rate_limiter_cleanup_loop())

    yield  # App runs here

    await mgr.save_matches()  # Save matches on shutdown

    match_cleanup_task.cancel()
    rate_limiter_cleanup_task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_home():
    """Serve the match creation page."""
    return FileResponse("static/index.html")

@app.get("/archive")
async def serve_archive():
    """Serve the archive page."""
    return FileResponse("static/archive.html")

@app.get("/favicon.ico")
async def serve_favicon():
    """Serve the site favicon."""
    return FileResponse("static/icons/favicon.ico")


@app.post("/create_match")
@rate_limit(max_requests=20, window_seconds=3600)
async def create_match(request: Request):
    """Creates a new match and returns admin & viewer links."""

    form = await request.form()
    match_id, admin_token = await mgr.create_match(form)

    base_url = f"{request.url.scheme}://{request.url.netloc}"
    admin_link = f"{base_url}/scoreboard/{match_id}?token={admin_token}"
    viewer_link = f"{base_url}/scoreboard/{match_id}"

    client_ip = request.client.host
    logging.info("%s Created match %s", client_ip, match_id)

    return {"admin_link": admin_link, "viewer_link": viewer_link}


@app.get("/scoreboard/{match_id}")
async def serve_scoreboard(request: Request, match_id: str):
    """Serve the scoreboard page."""
    match = mgr.get_match(match_id)
    if not match:
        logging.warning("Match %s does not exist. Redirecting.", match_id)
        return RedirectResponse("/")

    if match["ended"]:
        logging.info("Redirecting to match archive page for match %s", match_id)
        encoded_state = MatchManager.encode_match_state(match)
        archive_url = f"/archive?state={encoded_state}"
        return RedirectResponse(archive_url)

    return templates.TemplateResponse(
        name = "scoreboard.html",
        context = dict(match, request=request)
    )


@app.websocket("/ws/{match_id}")
async def websocket_endpoint(match_id: str, websocket: WebSocket, token: str = None):
    """Handles WebSocket connections for live score updates."""

    await websocket.accept()

    session_id = await mgr.try_add_session(match_id, websocket)
    if not session_id:
        return

    is_admin = token == mgr.get_match(match_id)["admin_token"]

    # Send initial match state
    await mgr.send_match_state(websocket, match_id)

    try:
        # Wait for updates
        while True:
            data = await websocket.receive_text()

            if not await mgr.validate_match(match_id, websocket):
                break

            if is_admin:
                update = json.loads(data)
                await mgr.process_admin_action(match_id, session_id, update)

    except (WebSocketDisconnect, RuntimeError) as e:
        logging.info("%s:%s disconnected (%s)", match_id, session_id, e)
    finally:
        await mgr.remove_session(match_id, session_id, websocket)
