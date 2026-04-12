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

    # Check if event_log exists to handle migrations
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='event_log'")
    table_exists = cursor.fetchone()

    if table_exists:
        # Check if the required columns exist
        cursor.execute("PRAGMA table_info(event_log)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'bowler' not in columns:
            cursor.execute("ALTER TABLE event_log ADD COLUMN bowler TEXT DEFAULT 'b1'")
        if 'match_id' not in columns:
            cursor.execute("ALTER TABLE event_log ADD COLUMN match_id INTEGER DEFAULT 1")
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                event_type TEXT,
                runs_added INTEGER,
                is_legal_ball BOOLEAN,
                batter TEXT,
                bowler TEXT,
                description TEXT,
                FOREIGN KEY (match_id) REFERENCES matches (id)
            )
        ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT DEFAULT 'active'
        )
    ''')

    # Create an initial match if none exists
    cursor.execute("SELECT id FROM matches WHERE status='active' ORDER BY id DESC LIMIT 1")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO matches (status) VALUES ('active')")

    conn.commit()
    conn.close()

init_db()

def get_current_match_id():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM matches WHERE status='active' ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_default_state():
    return {
        "runs": 0, "wickets": 0, "balls": 0,
        "target": 0,
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

game_state = get_default_state()

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

def build_state_for_match(match_id):
    state = get_default_state()
    if not match_id:
        return state

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT event_type, runs_added, is_legal_ball, batter, bowler, description FROM event_log WHERE match_id=? ORDER BY id ASC", (match_id,))
    events = cursor.fetchall()
    conn.close()

    for event in events:
        evt_type, runs, is_legal, evt_batter, evt_bowler, desc = event

        if evt_batter not in state["batter_stats"]:
            state["batter_stats"][evt_batter] = {"runs": 0, "balls": 0, "fours": 0}
        if evt_bowler not in state["bowler_stats"]:
            state["bowler_stats"][evt_bowler] = {"balls": 0, "runs": 0, "wickets": 0}

        if evt_type == "set_target":
            state["target"] = runs
        elif evt_type == "rename_batter":
            current_striker = state["striker"]
            stats = state["batter_stats"].pop(current_striker, {"runs": 0, "balls": 0, "fours": 0})
            state["batter_stats"][evt_batter] = stats
            state["striker"] = evt_batter
        elif evt_type == "change_bowler":
            state["bowler"] = evt_bowler
        elif evt_type == "dot":
            state["balls"] += 1
            state["batter_stats"][evt_batter]["balls"] += 1
            state["bowler_stats"][evt_bowler]["balls"] += 1
        elif evt_type == "run":
            state["runs"] += runs
            state["balls"] += 1
            state["batter_stats"][evt_batter]["runs"] += runs
            state["batter_stats"][evt_batter]["balls"] += 1
            if runs == 4:
                state["batter_stats"][evt_batter]["fours"] += 1
            state["bowler_stats"][evt_bowler]["runs"] += runs
            state["bowler_stats"][evt_bowler]["balls"] += 1
        elif evt_type == "no_ball":
            state["runs"] += 1
            state["bowler_stats"][evt_bowler]["runs"] += 1
        elif evt_type == "no_ball_hit":
            state["runs"] += 2
            state["batter_stats"][evt_batter]["runs"] += 1
            state["bowler_stats"][evt_bowler]["runs"] += 2
        elif evt_type == "2nd_bounce":
            state["runs"] += 1
            state["bowler_stats"][evt_bowler]["runs"] += 1
        elif evt_type == "wicket":
            state["wickets"] += 1
            state["balls"] += 1
            state["batter_stats"][evt_batter]["balls"] += 1
            state["bowler_stats"][evt_bowler]["balls"] += 1
            state["bowler_stats"][evt_bowler]["wickets"] += 1
            next_player = f"p{state['next_player_index']}"
            state["striker"] = next_player
            state["next_player_index"] += 1
            if next_player not in state["batter_stats"]:
                state["batter_stats"][next_player] = {"runs": 0, "balls": 0, "fours": 0}

        # Logs handling (keep last 10)
        state["logs"].insert(0, desc)
        if len(state["logs"]) > 10:
            state["logs"].pop()

    return state

def rebuild_state():
    global game_state
    match_id = get_current_match_id()
    game_state = build_state_for_match(match_id)

rebuild_state()

def log_event_to_db(event_type, runs_added, is_legal_ball, batter, bowler, description):
    match_id = get_current_match_id()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO event_log (match_id, event_type, runs_added, is_legal_ball, batter, bowler, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (match_id, event_type, runs_added, is_legal_ball, batter, bowler, description)
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

    if action == "end_match":
        match_id = get_current_match_id()
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if match_id:
            cursor.execute("UPDATE matches SET status='completed' WHERE id=?", (match_id,))
        cursor.execute("INSERT INTO matches (status) VALUES ('active')")
        conn.commit()
        conn.close()
        rebuild_state()
        return

    if action == "undo":
        match_id = get_current_match_id()
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM event_log WHERE match_id=? ORDER BY id DESC LIMIT 1", (match_id,))
        last_event = cursor.fetchone()
        if last_event:
            cursor.execute("DELETE FROM event_log WHERE id=?", (last_event[0],))
            conn.commit()
        conn.close()
        rebuild_state()
        return

    ensure_player_stats(batter, bowler)

    if action == "set_target":
        target_score = action_data.get("target_score", 0)
        game_state["target"] = target_score
        log_event_to_db("set_target", target_score, False, batter, bowler, f"Target set to {target_score}.")
        return

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

@app.get("/api/stats")
async def get_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, status FROM matches ORDER BY id DESC")
    matches = cursor.fetchall()
    conn.close()

    history = []
    all_time_batters = {}
    all_time_bowlers = {}

    for match in matches:
        m_id, status = match
        state = build_state_for_match(m_id)

        # Only add to history if it has some activity
        if state["balls"] > 0 or state["runs"] > 0:
            history.append({
                "match_id": m_id,
                "status": status,
                "runs": state["runs"],
                "wickets": state["wickets"],
                "balls": state["balls"]
            })

        # Aggregate all-time stats
        for b_name, s in state["batter_stats"].items():
            if b_name.startswith("p") and len(b_name) <= 3 and s["balls"] == 0:
                continue # Skip untouched default placeholders
            if b_name not in all_time_batters:
                all_time_batters[b_name] = {"runs": 0, "balls": 0, "fours": 0}
            all_time_batters[b_name]["runs"] += s["runs"]
            all_time_batters[b_name]["balls"] += s["balls"]
            all_time_batters[b_name]["fours"] += s["fours"]

        for b_name, s in state["bowler_stats"].items():
            if b_name.startswith("b") and len(b_name) <= 3 and s["balls"] == 0:
                continue
            if b_name not in all_time_bowlers:
                all_time_bowlers[b_name] = {"balls": 0, "runs": 0, "wickets": 0}
            all_time_bowlers[b_name]["balls"] += s["balls"]
            all_time_bowlers[b_name]["runs"] += s["runs"]
            all_time_bowlers[b_name]["wickets"] += s["wickets"]

    return {
        "history": history,
        "leaderboard_batters": all_time_batters,
        "leaderboard_bowlers": all_time_bowlers
    }

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