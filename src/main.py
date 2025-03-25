from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
import asyncio
import json
import uuid

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

matches = {}
connections = {}

@app.get("/")
async def serve_home():
    """Serve the match creation page."""
    return FileResponse("static/index.html")

@app.post("/create_match")
async def create_match(request: Request):
    """Creates a new match and returns admin & viewer links."""
    form = await request.form()
    teamA_name = form.get("teamA_name", "Team A")
    teamB_name = form.get("teamB_name", "Team B")
    teamA_color = form.get("teamA_color", "red")
    teamB_color = form.get("teamB_color", "blue")

    match_id = str(uuid.uuid4())[:8]
    admin_token = str(uuid.uuid4())[:8]

    matches[match_id] = {
        "score": {"teamA": 0, "teamB": 0},
        "teamA_name": teamA_name,
        "teamB_name": teamB_name,
        "teamA_color": teamA_color,
        "teamB_color": teamB_color,
        "admin_token": admin_token
    }
    connections[match_id] = []

    admin_link = f"/scoreboard/{match_id}?token={admin_token}"
    viewer_link = f"/scoreboard/{match_id}"

    return {"admin_link": admin_link, "viewer_link": viewer_link}

@app.get("/scoreboard/{match_id}")
async def serve_scoreboard(match_id: str):
    """Serve the scoreboard page."""
    if match_id not in matches:
        return RedirectResponse("/")
    return FileResponse("static/scoreboard.html")

@app.websocket("/ws/{match_id}")
async def websocket_endpoint(match_id: str, websocket: WebSocket, token: str = None):
    """WebSocket for real-time updates."""
    await websocket.accept()

    if match_id not in matches:
        await websocket.close()
        return

    is_admin = token == matches[match_id]["admin_token"]
    connections[match_id].append(websocket)

    try:
        await websocket.send_json(matches[match_id])

        while True:
            data = await websocket.receive_text()

            if is_admin:
                update = json.loads(data)

                if update["action"] == "increment":
                    matches[match_id]["score"][update["team"]] += 1
                elif update["action"] == "decrement":
                    if matches[match_id]["score"][update["team"]] > 0:
                        matches[match_id]["score"][update["team"]] -= 1
                elif update["action"] == "reset":
                    matches[match_id]["score"] = {"teamA": 0, "teamB": 0}

                for conn in connections[match_id]:
                    await conn.send_json(matches[match_id])

    except WebSocketDisconnect:
        connections[match_id].remove(websocket)
