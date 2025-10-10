"""Manages access to in-memory match data in a thread-safe manner via the MatchManager class"""

import asyncio
import logging
import time
import secrets
import html
import os
import json
import base64
import zlib
import uuid
from typing import Dict#, Optional
#from copy import deepcopy
from datetime import datetime
from pathlib import Path
from starlette.datastructures import FormData
from fastapi import WebSocket, status
from exceptions import MatchNotFoundError, MatchEndedError

class MatchManager:
    """Manages access to in-memory match data in a thread-safe manner"""

    MATCH_EXPIRY_TIME = 3 * 60 * 60  # 3 hours in seconds

    def __init__(self):
        self._matches: Dict[str, dict] = {}
        self._match_static: Dict[str, dict] = {}
        self._sessions: Dict[str, dict] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._matches_file = Path(os.getenv("MATCHES_FILE", "/opt/vbscore-data/matches.json"))


    async def save_matches(self):
        """Save matches to disk."""
        try:
            logging.info("Saving %s matches to disk.", len(self._matches))
            with self._matches_file.open("w", encoding="utf-8") as f:
                json.dump([self._matches,self._match_static], f, ensure_ascii=False, indent=4)
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("Error saving matches: %s", e)


    async def load_matches(self):
        """Load matches from disk."""
        if self._matches_file.exists():
            async with self._global_lock:
                try:
                    with self._matches_file.open("r", encoding="utf-8") as f:
                        md,ms = json.load(f)
                        self._matches.update(md)
                        self._match_static.update(ms)
                    for match_id,_ in self._matches.items():
                        self._sessions[match_id] = []
                    logging.info("%s matches loaded from disk.", len(self._matches))
                except Exception as e:  # pylint: disable=broad-except
                    logging.warning("Error loading matches: %s", e)
        else:
            logging.info("No existing matches file found at %s.", self._matches_file)


    async def cleanup_matches(self):
        """Removes matches that haven't been updated in the last 3 hours."""
        while True:
            current_time = time.time()
            to_delete = [
                match_id for match_id, match in self._matches.items()
                if (current_time - match["last_updated"]) > self.MATCH_EXPIRY_TIME
            ]
            async with self._global_lock:
                for match_id in to_delete:
                    del self._matches[match_id]
                    del self._match_static[match_id]
                    del self._sessions[match_id]
                    logging.info("Match %s expired and was removed.", match_id)
            await asyncio.sleep(600)  # Run cleanup every 10 minutes


    def _get_lock(self, match_id: str) -> asyncio.Lock:
        if match_id not in self._locks:
            self._locks[match_id] = asyncio.Lock()
        return self._locks[match_id]


    def _relative_luminance(self, r, g, b):
        """Compute relative luminance as per WCAG 2.1."""
        def adjust(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = adjust(r), adjust(g), adjust(b)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b


    def _get_contrast_color(self, hex_color: str) -> str:
        """Returns black (#000000) or white (#FFFFFF) based on WCAG contrast ratio."""
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))

        # Calculate contrast ratio against white and black
        lum_bg = self._relative_luminance(r, g, b)
        lum_white = self._relative_luminance(255, 255, 255)
        lum_black = self._relative_luminance(0, 0, 0)

        contrast_white = (lum_white + 0.05) / (lum_bg + 0.05)
        contrast_black = (lum_bg + 0.05) / (lum_black + 0.05)

        # Choose the text color that provides better contrast
        return "#FFFFFF" if contrast_white > contrast_black else "#000000"


    def get_match(self,match_id):
        """Retrieve dynamic match data by ID. Returns None if it doesn't exist."""
        return self._matches.get(match_id)


    def get_match_static(self,match_id):
        """Retrieve static match data by ID. Returns None if it doesn't exist."""
        return self._match_static.get(match_id)


    async def validate_match(self, match_id: str, websocket: WebSocket) -> bool:
        """Check if match exists; redirect and close if it doesn't. Return True if still valid."""
        if not self.get_match(match_id):
            await self.send_redirect(websocket, "/")
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            return False
        return True


    def encode_match_state(self, match_id):
        """Encodes a match state into a structured JSON object for archiving."""
        match = self.get_match(match_id)
        match_static = self.get_match_static(match_id)
        match_state = {
            "v": 2,  # schema version
            "d": int(datetime.now().timestamp()),  # epoch timestamp
            "l": match_static["desc"],
            "a": {
                "n": match_static["a"]["name"],
                "b": match_static["a"]["color_bg"],
                "f": match_static["a"]["color_fg"],
            },
            "b": {
                "n": match_static["b"]["name"],
                "b": match_static["b"]["color_bg"],
                "f": match_static["b"]["color_fg"],
            },
            "h": match["history"]
        }

        # Convert to JSON string
        json_str = json.dumps(match_state, separators=(",", ":"))
        logging.debug(json_str)

        # Compress and Encode as Base64Url (without padding)
        compressed = zlib.compress(json_str.encode())
        encoded_state = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        return encoded_state


    async def create_match(self, form: FormData) -> str:
        """Create a new match based on form data"""
        a_color_bg = form.get("a_color", "#FF0000") # red default
        b_color_bg = form.get("b_color", "#0000FF") # blue default
        admin_token = secrets.token_urlsafe(6)
        match_data = {
            "history": [[]],
            "last_updated": time.time(),
            "done": False,
            "viewers": 0,
        }
        match_static = {
            "a": {
                "name": html.escape(form.get("a_name", "Team A")[:20]),
                "color_bg": a_color_bg,
                "color_fg": self._get_contrast_color(a_color_bg),
            },
            "b": {
                "name": html.escape(form.get("b_name", "Team B")[:20]),
                "color_bg": b_color_bg,
                "color_fg": self._get_contrast_color(b_color_bg),
            },
            "desc": html.escape(form.get("mLoc", "")[:30]),
            "admin_token": admin_token,
        }
        async with self._global_lock:
            # generate an unused match ID
            match_id = None
            while not match_id or match_id in self._matches:
                match_id = secrets.token_urlsafe(6)
            # add the match dynamic and static data, empty session list, and shared lock
            self._matches[match_id] = match_data
            self._match_static[match_id] = match_static
            self._sessions[match_id] = []
            self._locks[match_id] = asyncio.Lock()

        return match_id,admin_token


    async def add_session(self, match_id: str, ws: WebSocket) -> str:
        """Add a new WebSocket session. Returns the session_id."""
        match = self.get_match(match_id)
        if not match:
            raise MatchNotFoundError(f"Match {match_id} not found.")

        if match["done"]:
            raise MatchEndedError(f"Match {match_id} has already ended.")

        session_id = str(uuid.uuid4())[:8]

        async with self._get_lock(match_id):
            self._sessions[match_id].append({
                "session_id": session_id,
                "websocket": ws
            })
            match["viewers"] = len(self._sessions[match_id])

        logging.info("%s:%s WebSocket connection opened", match_id, session_id)
        return session_id


    async def try_add_session(self, match_id: str, websocket: WebSocket) -> str | None:
        """Try to add a session, or send a redirect and return None."""
        try:
            return await self.add_session(match_id, websocket)
        except MatchNotFoundError:
            await self.send_redirect(websocket, "/")
        except MatchEndedError:
            archive_url = f"/archive?state={self.encode_match_state(match_id)}"
            await self.send_redirect(websocket, archive_url)
        return None


    async def remove_session(self, match_id: str, session_id: str, ws: WebSocket):
        """Remove a WebSocket session from a match."""
        async with self._get_lock(match_id):
            if match_id in self._sessions:
                logging.info("%s:%s WebSocket connection closing", match_id, session_id)
                self._sessions[match_id] = [
                    s for s in self._sessions[match_id]
                    if s["websocket"] != ws
                ]
            if match_id in self._matches:
                self._matches[match_id]["viewers"] = len(self._sessions[match_id])
        try:
            await ws.close(code=status.WS_1000_NORMAL_CLOSURE)
        except Exception:  # pylint: disable=broad-except
            pass  # Ignore if already closed


    async def process_admin_action(self, match_id, session_id, update):
        """Process admin actions and return True if the match ended."""
        match = self._matches[match_id]

        # sanitize inputs
        action = update.get('action')
        if not action:
            logging.warning("%s:%s Action missing from update.",
                            match_id, session_id)
            return

        if action == 'point':
            team = update.get('team')
            if team != 0 and team != 1: # pylint: disable=consider-using-in
                logging.warning("%s:%s Invalid team sent for increment/decrement: %s",
                                match_id, session_id, team)
                return

            async with self._get_lock(match_id):
                current_set = match["history"][-1]
                team_score = current_set.count(team)

                if team_score < 99:
                    current_set.append(team)
                    match["last_updated"] = time.time()
                else:
                    logging.info("%s:%s Team %d already has 99 points, not adding more", match_id, session_id, team)

        elif action == "undo":
            async with self._get_lock(match_id):
                if match["history"][-1]:
                    removed_team = match["history"][-1].pop()
                    match["last_updated"] = time.time()
                else:
                    logging.info("%s:%s Undo ignored — no points to remove", match_id, session_id)

        elif action == 'new_set' or action == 'end_match': # pylint: disable=consider-using-in
            async with self._get_lock(match_id):
                match["last_updated"] = time.time()
                match["done"] = action == "end_match" or len(match["history"]) >= 5
                if not match["done"]:
                    match["history"].append([])
                    logging.info("%s:%s New set", match_id, session_id)
                else:
                    # delete the final set if it's empty (like they accidentally clicked
                    # new set and then clicked end match)
                    if not match["history"][-1]:
                        match["history"].pop()

                    # send everyone to the archive
                    logging.info("%s:%s Match ended", match_id, session_id)
                    archive_url = f"/archive?state={self.encode_match_state(match_id)}"
                    await self.broadcast_redirect(match_id, archive_url)
                    return

        else:
            logging.warning("%s:%s Unrecognized action sent in update: %s",
                            match_id, session_id, action)

        if not match["done"]:
            await self.broadcast_match_state(match_id)


    async def broadcast_match_state(self, match_id):
        """Send the updated game state to all connected clients."""
        match = self.get_match(match_id)
        if match:
            for session in self._sessions.get(match_id, []):
                await session["websocket"].send_json(match)


    async def send_redirect(self, websocket: WebSocket, url: str):
        """Send a redirect command and close the WebSocket."""
        try:
            await websocket.send_json({"redirect": url})
        except Exception: # pylint: disable=broad-except
            pass  # Ignore send errors
        finally:
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)

    async def broadcast_redirect(self, match_id: str, url: str):
        """Send a redirect to all clients for a match."""
        logging.info("%s Sending redirect to all clients.", match_id)
        for session in self._sessions.get(match_id, []):
            await self.send_redirect(session["websocket"], url)

    async def send_match_state(self, websocket: WebSocket, match_id: str):
        """Send the current match state to a connected client."""
        match = self.get_match(match_id)
        if match:
            await websocket.send_json(match)
