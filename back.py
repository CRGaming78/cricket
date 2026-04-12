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
            bowler TEXT,
            description TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

game_state = {
    "runs": 0, "wickets": 0, "balls": 0,
    "striker": "p1",
    "bowler": "b1",
    "next_player_index": 2,
    "logs": [],
    "batter_stats": {
        "p1": {"runs": 0, "balls": 0, "fours": 0}
    },
    "bowler_stats": {
        "b1": {"balls": 0, "runs": 0, "wickets": 0}
    }
}

def ensure_player_stats(batter, bowler):
    if batter not in game_state["batter_stats"]:
        game_state["batter_stats"][batter] = {"runs": 0, "balls": 0, "fours": 0}
    if bowler not in game_state["bowler_stats"]:
        game_state["bowler_stats"][bowler] = {"balls": 0, "runs": 0, "wickets": 0}

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

def log_event_to_db(event_type, runs_added, is_legal_ball, batter, bowler, description):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO event_log (event_type, runs_added, is_legal_ball, batter, bowler, description) VALUES (?, ?, ?, ?, ?, ?)",
        (event_type, runs_added, is_legal_ball, batter, bowler, description)
    )
    conn.commit()
    conn.close()
    
    game_state["logs"].insert(0, description)
    if len(game_state["logs"]) > 10:
        game_state["logs"].pop()

def process_action(action_data):
    action = action_data.get("action")
    batter = game_state["striker"]
    bowler = game_state["bowler"]

    ensure_player_stats(batter, bowler)

    if action == "rename_batter":
        new_name = action_data.get("new_name")
        if new_name:
            stats = game_state["batter_stats"].pop(batter, {"runs": 0, "balls": 0, "fours": 0})
            game_state["batter_stats"][new_name] = stats
            game_state["striker"] = new_name
            log_event_to_db("rename_batter", 0, False, new_name, bowler, f"Batter renamed to {new_name}.")
        return

    if action == "change_bowler":
        new_name = action_data.get("new_name")
        if new_name:
            game_state["bowler"] = new_name
            ensure_player_stats(batter, new_name)
            log_event_to_db("change_bowler", 0, False, batter, new_name, f"Bowler changed to {new_name}.")
        return
    
    if action == "dot":
        game_state["balls"] += 1
        game_state["batter_stats"][batter]["balls"] += 1
        game_state["bowler_stats"][bowler]["balls"] += 1
        log_event_to_db("dot", 0, True, batter, bowler, f"{batter} played a dot ball.")
    elif action == "1_run":
        game_state["runs"] += 1
        game_state["balls"] += 1
        game_state["batter_stats"][batter]["runs"] += 1
        game_state["batter_stats"][batter]["balls"] += 1
        game_state["bowler_stats"][bowler]["runs"] += 1
        game_state["bowler_stats"][bowler]["balls"] += 1
        log_event_to_db("run", 1, True, batter, bowler, f"{batter} scored 1 run.")
    elif action == "4_runs":
        game_state["runs"] += 4
        game_state["balls"] += 1
        game_state["batter_stats"][batter]["runs"] += 4
        game_state["batter_stats"][batter]["balls"] += 1
        game_state["batter_stats"][batter]["fours"] += 1
        game_state["bowler_stats"][bowler]["runs"] += 4
        game_state["bowler_stats"][bowler]["balls"] += 1
        log_event_to_db("run", 4, True, batter, bowler, f"{batter} hit a 4!")
    elif action == "no_ball":
        game_state["runs"] += 1
        game_state["bowler_stats"][bowler]["runs"] += 1
        log_event_to_db("no_ball", 1, False, batter, bowler, "No Ball! 1 Run added.")
    elif action == "no_ball_hit":
        game_state["runs"] += 2
        game_state["batter_stats"][batter]["runs"] += 1 # batter gets the run off the bat, noball is 1 extra
        game_state["bowler_stats"][bowler]["runs"] += 2
        log_event_to_db("no_ball_hit", 2, False, batter, bowler, f"No Ball + Hit! {batter} gets 1 run.")
    elif action == "2nd_bounce":
        game_state["runs"] += 1
        game_state["bowler_stats"][bowler]["runs"] += 1
        log_event_to_db("2nd_bounce", 1, False, batter, bowler, "2nd Bounce No Ball! 1 Run added.")
    elif action in ["one_hand_out", "out_of_fence", "standard_out"]:
        game_state["wickets"] += 1
        game_state["balls"] += 1
        game_state["batter_stats"][batter]["balls"] += 1
        game_state["bowler_stats"][bowler]["balls"] += 1
        game_state["bowler_stats"][bowler]["wickets"] += 1
        labels = {"one_hand_out": "One Hand Out", "out_of_fence": "Hit Out of Fence", "standard_out": "Wicket"}
        log_event_to_db("wicket", 0, True, batter, bowler, f"OUT! {batter} is gone ({labels[action]}).")

        # Next batter logic
        next_player = f"p{game_state['next_player_index']}"
        game_state["striker"] = next_player
        game_state["next_player_index"] += 1
        ensure_player_stats(next_player, bowler)

    if action_data.get("action") != "reset" and game_state["balls"] > 0 and game_state["balls"] % 6 == 0 and action in ["dot", "1_run", "4_runs", "one_hand_out", "out_of_fence", "standard_out"]:
        log_event_to_db("over", 0, False, batter, bowler, "End of Over.")

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