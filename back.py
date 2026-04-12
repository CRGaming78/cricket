import sqlite3
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import List

app = FastAPI()

ADMIN_PIN = "1234"
DB_FILE = "gully_cricket.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            runs_added INTEGER,
            is_legal_ball BOOLEAN,
            batter TEXT,
            description TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

game_state = {
    "runs": 0, "wickets": 0, "balls": 0,
    "striker": "p1", "non_striker": "p2",
    "next_player_index": 3, "logs": []
}

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        await websocket.send_json(game_state)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast_state(self):
        for connection in self.active_connections:
            await connection.send_json(game_state)

manager = ConnectionManager()

def log_event_to_db(event_type, runs_added, is_legal_ball, batter, description):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO event_log (event_type, runs_added, is_legal_ball, batter, description) VALUES (?, ?, ?, ?, ?)",
        (event_type, runs_added, is_legal_ball, batter, description)
    )
    conn.commit()
    conn.close()
    
    game_state["logs"].insert(0, description)
    if len(game_state["logs"]) > 10:
        game_state["logs"].pop()

def process_action(action_data):
    action = action_data.get("action")
    batter = game_state["striker"]
    
    if action == "dot":
        game_state["balls"] += 1
        log_event_to_db("dot", 0, True, batter, f"{batter} played a dot ball.")
    elif action == "1_run":
        game_state["runs"] += 1
        game_state["balls"] += 1
        log_event_to_db("run", 1, True, batter, f"{batter} scored 1 run.")
        game_state["striker"], game_state["non_striker"] = game_state["non_striker"], game_state["striker"]
    elif action == "no_ball":
        game_state["runs"] += 1
        log_event_to_db("no_ball", 1, False, batter, "No Ball! 1 Run added.")
    elif action == "no_ball_hit":
        game_state["runs"] += 2
        log_event_to_db("no_ball_hit", 2, False, batter, f"No Ball + Hit! {batter} gets 2 runs.")
    elif action == "2nd_bounce":
        game_state["runs"] += 1
        log_event_to_db("2nd_bounce", 1, False, batter, "2nd Bounce No Ball! 1 Run added.")
    elif action in ["one_hand_out", "out_of_fence", "standard_out"]:
        game_state["wickets"] += 1
        game_state["balls"] += 1
        labels = {"one_hand_out": "One Hand Out", "out_of_fence": "Hit Out of Fence", "standard_out": "Wicket"}
        log_event_to_db("wicket", 0, True, batter, f"OUT! {batter} is gone ({labels[action]}).")
        game_state["striker"] = f"p{game_state['next_player_index']}"
        game_state["next_player_index"] += 1

    if action_data.get("action") != "reset" and game_state["balls"] > 0 and game_state["balls"] % 6 == 0:
        game_state["striker"], game_state["non_striker"] = game_state["non_striker"], game_state["striker"]
        log_event_to_db("over", 0, False, "system", "End of Over. Strike rotated.")

@app.get("/")
async def get():
    with open("index.html", "r") as f:
        return HTMLResponse(f.read())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            action_data = json.loads(data)
            
            if action_data.get("pin") != ADMIN_PIN:
                await websocket.send_json({"type": "error", "message": "Unauthorized: Wrong PIN"})
                continue
                
            process_action(action_data)
            await manager.broadcast_state()
    except WebSocketDisconnect:
        manager.disconnect(websocket)