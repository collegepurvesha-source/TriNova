import asyncio
import json
import logging
import hashlib
import mimetypes
import os
import secrets
import time
from datetime import datetime
import websockets
from websockets.http11 import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ROOMS_FILE = os.path.join(DATA_DIR, "rooms.json")
MESSAGES_DIR = os.path.join(DATA_DIR, "messages")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MESSAGES_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)

def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def hash_password(pw):
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def generate_room_code():
    return secrets.token_hex(3).upper()  # 6-char hex code

# ─────────────────────────────────────────────
# Data Store  (loaded on startup, saved on mutation)
# ─────────────────────────────────────────────

# users_db: { username_lower: { "username": str, "password_hash": str } }
users_db = {}

# rooms_db: { room_path: { "name": str, "code": str, "creator": str,
#              "admins": [str], "moderators": [str], "members": [str],
#              "pending": [str], "parent": str|null } }
rooms_db = {}

def save_users():
    save_json(USERS_FILE, users_db)

def save_rooms():
    save_json(ROOMS_FILE, rooms_db)

def room_messages_path(room_path):
    safe = room_path.replace("/", "__")
    return os.path.join(MESSAGES_DIR, f"{safe}.json")

def load_room_messages(room_path, limit=100):
    p = room_messages_path(room_path)
    msgs = load_json(p, [])
    return msgs[-limit:]

def append_room_message(room_path, msg):
    p = room_messages_path(room_path)
    msgs = load_json(p, [])
    msgs.append(msg)
    # Keep at most 500 messages per room
    if len(msgs) > 500:
        msgs = msgs[-500:]
    save_json(p, msgs)

# ─────────────────────────────────────────────
# Runtime State
# ─────────────────────────────────────────────

# clients: {ws -> {"username": str, "room": str|None}}
clients = {}
# username_map: {username_lower -> ws}
username_map = {}
# Active room occupants: {room_path -> set of ws}
room_occupants = {}

ROLE_SYMBOLS = {"admin": "👑", "moderator": "🛡️", "member": "👤"}

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def now_ts():
    return datetime.now().strftime("%H:%M")

def now_full():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def send(ws, msg: dict):
    try:
        await ws.send(json.dumps(msg))
    except Exception:
        pass

async def send_system(ws, content: str):
    await send(ws, {"type": "system", "content": content, "ts": now_ts()})

async def broadcast_room(room_path: str, msg: dict, exclude=None):
    if room_path not in room_occupants:
        return
    tasks = [send(ws, msg) for ws in room_occupants[room_path] if ws != exclude]
    if tasks:
        await asyncio.gather(*tasks)

async def broadcast_system_room(room_path: str, content: str, exclude=None):
    await broadcast_room(room_path, {"type": "system", "content": content, "ts": now_ts()}, exclude=exclude)

def get_ws_by_name(username: str):
    return username_map.get(username.lower())

def get_user_role_in_room(username: str, room_path: str):
    room = rooms_db.get(room_path)
    if not room:
        return None
    ulow = username.lower()
    if ulow in [a.lower() for a in room["admins"]]:
        return "admin"
    if ulow in [m.lower() for m in room["moderators"]]:
        return "moderator"
    if ulow in [m.lower() for m in room["members"]]:
        return "member"
    return None

ROLE_POWER = {"admin": 3, "moderator": 2, "member": 1}

def get_child_rooms(parent_path):
    """Get direct child rooms of a parent."""
    children = []
    for rp, rd in rooms_db.items():
        if rd.get("parent") == parent_path:
            children.append(rp)
    return sorted(children)

def get_room_tree():
    """Build a tree structure of all rooms for the sidebar."""
    tree = []
    # Top-level rooms (no parent)
    top_rooms = sorted([rp for rp, rd in rooms_db.items() if not rd.get("parent")])
    for rp in top_rooms:
        tree.append(_build_tree_node(rp))
    return tree

def _build_tree_node(room_path):
    rd = rooms_db[room_path]
    children_paths = get_child_rooms(room_path)
    children = [_build_tree_node(cp) for cp in children_paths]
    return {
        "path": room_path,
        "name": rd["name"],
        "children": children
    }

# ─────────────────────────────────────────────
# Push updates
# ─────────────────────────────────────────────

async def push_room_tree(ws):
    tree = get_room_tree()
    client = clients.get(ws)
    if not client:
        return
    current = client.get("room")
    # Also send which rooms this user is a member of
    ulow = client["username"].lower()
    my_rooms = []
    for rp, rd in rooms_db.items():
        all_members = [a.lower() for a in rd["admins"]] + [m.lower() for m in rd["moderators"]] + [m.lower() for m in rd["members"]]
        if ulow in all_members:
            my_rooms.append(rp)
    await send(ws, {
        "type": "room_tree",
        "tree": tree,
        "current_room": current,
        "my_rooms": my_rooms
    })

async def push_room_tree_to_all():
    tasks = [push_room_tree(ws) for ws in list(clients)]
    if tasks:
        await asyncio.gather(*tasks)

async def push_user_list(room_path: str):
    if room_path not in room_occupants:
        return
    user_list = []
    for ws in room_occupants[room_path]:
        if ws in clients:
            uname = clients[ws]["username"]
            role = get_user_role_in_room(uname, room_path) or "member"
            user_list.append({"name": uname, "role": role})
    msg = {"type": "user_list", "users": user_list, "room": room_path}
    await broadcast_room(room_path, msg)

async def push_pending_list(ws, room_path: str):
    """Send pending join requests to an admin."""
    room = rooms_db.get(room_path)
    if not room:
        return
    await send(ws, {
        "type": "pending_list",
        "room": room_path,
        "pending": room.get("pending", [])
    })

async def push_pending_to_admins(room_path: str):
    room = rooms_db.get(room_path)
    if not room:
        return
    for admin_name in room["admins"]:
        aws = get_ws_by_name(admin_name)
        if aws and aws in clients and clients[aws].get("room") == room_path:
            await push_pending_list(aws, room_path)

# ─────────────────────────────────────────────
# Room join / leave
# ─────────────────────────────────────────────

async def enter_room(ws, room_path: str):
    """Move client into a room (already authorized)."""
    client = clients[ws]
    old_room = client["room"]

    # Leave old room
    if old_room and old_room in room_occupants:
        room_occupants[old_room].discard(ws)
        await broadcast_system_room(old_room, f"🚪 {client['username']} left the room")
        await push_user_list(old_room)
        if not room_occupants[old_room]:
            del room_occupants[old_room]

    # Join new room
    if room_path not in room_occupants:
        room_occupants[room_path] = set()
    room_occupants[room_path].add(ws)
    client["room"] = room_path

    room = rooms_db.get(room_path, {})
    role = get_user_role_in_room(client["username"], room_path) or "member"

    # Send history
    history = load_room_messages(room_path)
    await send(ws, {
        "type": "room_joined",
        "room": room_path,
        "room_name": room.get("name", room_path),
        "role": role,
        "history": history,
        "room_code": room.get("code", "") if role == "admin" else "",
        "ts": now_ts()
    })

    await broadcast_system_room(room_path, f"✅ {client['username']} joined the room", exclude=ws)
    await push_user_list(room_path)
    await push_room_tree_to_all()

    # If admin, send pending list
    if role == "admin":
        await push_pending_list(ws, room_path)

# ─────────────────────────────────────────────
# Command Handlers
# ─────────────────────────────────────────────

async def cmd_help(ws, _args):
    client = clients[ws]
    room_path = client["room"]
    role = get_user_role_in_room(client["username"], room_path) if room_path else None
    rp = ROLE_POWER.get(role, 0)

    lines = [
        "📖 <b>Available Commands</b>",
        "/help — show this message",
        "/users — list users in current room",
    ]
    if rp >= 2:
        lines += [
            "/kick &lt;user&gt; — kick a user from the room",
            "/announce &lt;message&gt; — broadcast announcement",
        ]
    if rp >= 3:
        lines += [
            "/makemod &lt;user&gt; — promote user to moderator",
            "/makeadmin &lt;user&gt; — promote user to admin",
            "/demote &lt;user&gt; — demote user",
            "/delete — delete this room",
        ]
    await send_system(ws, "\n".join(lines))


async def cmd_users(ws, _args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    occupants = room_occupants.get(room_path, set())
    user_list = []
    for w in occupants:
        if w in clients:
            uname = clients[w]["username"]
            role = get_user_role_in_room(uname, room_path) or "member"
            user_list.append(f"{ROLE_SYMBOLS.get(role, '👤')} {uname}")
    await send_system(ws, f"👥 <b>Users in room:</b> " + ", ".join(user_list))


async def cmd_kick(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 2:
        await send_system(ws, "❌ You do not have permission to kick users.")
        return
    if not args:
        await send_system(ws, "❌ Usage: /kick &lt;username&gt;")
        return

    target_name = args[0]
    target_ws = get_ws_by_name(target_name)
    if not target_ws or target_ws not in clients:
        await send_system(ws, f"❌ User '{target_name}' not found or not online.")
        return
    if target_ws not in room_occupants.get(room_path, set()):
        await send_system(ws, f"❌ {target_name} is not in this room.")
        return

    target_role = get_user_role_in_room(target_name, room_path)
    if ROLE_POWER.get(my_role, 0) <= ROLE_POWER.get(target_role, 0):
        await send_system(ws, f"❌ You cannot kick {ROLE_SYMBOLS.get(target_role, '')} {target_name}.")
        return

    # Remove from room members
    room = rooms_db.get(room_path)
    if room:
        tlow = target_name.lower()
        room["members"] = [m for m in room["members"] if m.lower() != tlow]
        room["moderators"] = [m for m in room["moderators"] if m.lower() != tlow]
        save_rooms()

    await broadcast_system_room(room_path, f"🥾 <b>{target_name}</b> was kicked by {client['username']}.")
    room_occupants[room_path].discard(target_ws)
    clients[target_ws]["room"] = None
    await send(target_ws, {"type": "kicked", "room": room_path, "by": client["username"], "ts": now_ts()})
    await push_user_list(room_path)
    await push_room_tree_to_all()


async def cmd_announce(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 2:
        await send_system(ws, "❌ Only moderators and admins can make announcements.")
        return
    if not args:
        await send_system(ws, "❌ Usage: /announce &lt;message&gt;")
        return
    msg_text = " ".join(args)
    ann_msg = {
        "type": "announcement",
        "from": client["username"],
        "content": msg_text,
        "ts": now_ts()
    }
    await broadcast_room(room_path, ann_msg)
    # Save to history
    append_room_message(room_path, {**ann_msg, "full_ts": now_full()})


async def cmd_makemod(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 3:
        await send_system(ws, "❌ Only room admins can promote to moderator.")
        return
    if not args:
        await send_system(ws, "❌ Usage: /makemod &lt;username&gt;")
        return

    target_name = args[0]
    room = rooms_db.get(room_path)
    if not room:
        return

    tlow = target_name.lower()
    # Check they are a member
    all_members = [m.lower() for m in room["members"]] + [m.lower() for m in room["moderators"]] + [m.lower() for m in room["admins"]]
    if tlow not in all_members:
        await send_system(ws, f"❌ {target_name} is not a member of this room.")
        return
    if tlow in [a.lower() for a in room["admins"]]:
        await send_system(ws, f"ℹ️ {target_name} is already an admin.")
        return
    if tlow in [m.lower() for m in room["moderators"]]:
        await send_system(ws, f"ℹ️ {target_name} is already a moderator.")
        return

    # Find original case name
    orig_name = target_name
    for m in room["members"]:
        if m.lower() == tlow:
            orig_name = m
            break

    room["members"] = [m for m in room["members"] if m.lower() != tlow]
    room["moderators"].append(orig_name)
    save_rooms()

    await send_system(ws, f"✅ {orig_name} is now a moderator.")
    target_ws = get_ws_by_name(target_name)
    if target_ws and target_ws in clients:
        await send(target_ws, {"type": "role_update", "role": "moderator", "room": room_path, "ts": now_ts()})
        await send_system(target_ws, "🛡️ You have been promoted to <b>moderator</b>.")
    await push_user_list(room_path)
    await broadcast_system_room(room_path, f"🛡️ <b>{orig_name}</b> has been promoted to moderator.")


async def cmd_makeadmin(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 3:
        await send_system(ws, "❌ Only room admins can promote to admin.")
        return
    if not args:
        await send_system(ws, "❌ Usage: /makeadmin &lt;username&gt;")
        return

    target_name = args[0]
    room = rooms_db.get(room_path)
    if not room:
        return

    tlow = target_name.lower()
    all_members = [m.lower() for m in room["members"]] + [m.lower() for m in room["moderators"]] + [m.lower() for m in room["admins"]]
    if tlow not in all_members:
        await send_system(ws, f"❌ {target_name} is not a member of this room.")
        return
    if tlow in [a.lower() for a in room["admins"]]:
        await send_system(ws, f"ℹ️ {target_name} is already an admin.")
        return

    # Find original name
    orig_name = target_name
    for lst in [room["members"], room["moderators"]]:
        for m in lst:
            if m.lower() == tlow:
                orig_name = m
                break

    room["members"] = [m for m in room["members"] if m.lower() != tlow]
    room["moderators"] = [m for m in room["moderators"] if m.lower() != tlow]
    room["admins"].append(orig_name)
    save_rooms()

    await send_system(ws, f"✅ {orig_name} is now an admin.")
    target_ws = get_ws_by_name(target_name)
    if target_ws and target_ws in clients:
        await send(target_ws, {"type": "role_update", "role": "admin", "room": room_path, "ts": now_ts()})
        await send_system(target_ws, "👑 You have been promoted to <b>admin</b>!")
    await push_user_list(room_path)
    await broadcast_system_room(room_path, f"👑 <b>{orig_name}</b> has been promoted to admin.")


async def cmd_demote(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 3:
        await send_system(ws, "❌ Only room admins can demote users.")
        return
    if not args:
        await send_system(ws, "❌ Usage: /demote &lt;username&gt;")
        return

    target_name = args[0]
    room = rooms_db.get(room_path)
    if not room:
        return

    tlow = target_name.lower()
    if tlow == room["creator"].lower():
        await send_system(ws, "❌ Cannot demote the room creator.")
        return

    if tlow in [a.lower() for a in room["admins"]]:
        orig = next((a for a in room["admins"] if a.lower() == tlow), target_name)
        room["admins"] = [a for a in room["admins"] if a.lower() != tlow]
        room["members"].append(orig)
        save_rooms()
        await send_system(ws, f"✅ {orig} has been demoted to member.")
        target_ws = get_ws_by_name(target_name)
        if target_ws and target_ws in clients:
            await send(target_ws, {"type": "role_update", "role": "member", "room": room_path, "ts": now_ts()})
            await send_system(target_ws, "👤 You have been demoted to <b>member</b>.")
        await push_user_list(room_path)
        await broadcast_system_room(room_path, f"👤 <b>{orig}</b> has been demoted to member.")
        return

    if tlow in [m.lower() for m in room["moderators"]]:
        orig = next((m for m in room["moderators"] if m.lower() == tlow), target_name)
        room["moderators"] = [m for m in room["moderators"] if m.lower() != tlow]
        room["members"].append(orig)
        save_rooms()
        await send_system(ws, f"✅ {orig} has been demoted to member.")
        target_ws = get_ws_by_name(target_name)
        if target_ws and target_ws in clients:
            await send(target_ws, {"type": "role_update", "role": "member", "room": room_path, "ts": now_ts()})
            await send_system(target_ws, "👤 You have been demoted to <b>member</b>.")
        await push_user_list(room_path)
        await broadcast_system_room(room_path, f"👤 <b>{orig}</b> has been demoted to member.")
        return

    await send_system(ws, f"ℹ️ {target_name} is already a regular member.")


async def cmd_delete(ws, args):
    client = clients[ws]
    room_path = client["room"]
    if not room_path:
        await send_system(ws, "❌ You are not in any room.")
        return
    room = rooms_db.get(room_path)
    if not room:
        await send_system(ws, "❌ Room not found.")
        return
    my_role = get_user_role_in_room(client["username"], room_path)
    if ROLE_POWER.get(my_role, 0) < 3:
        await send_system(ws, "❌ Only room admins can delete rooms.")
        return

    # Check for sub-rooms
    children = get_child_rooms(room_path)
    if children:
        await send_system(ws, "❌ Delete all sub-rooms first before deleting this room.")
        return

    # Notify and eject everyone
    await broadcast_system_room(room_path, f"💥 Room <b>{room['name']}</b> has been deleted by {client['username']}.")
    occupants = list(room_occupants.get(room_path, set()))
    for occ_ws in occupants:
        if occ_ws in clients:
            clients[occ_ws]["room"] = None
            await send(occ_ws, {"type": "room_deleted", "room": room_path, "ts": now_ts()})

    if room_path in room_occupants:
        del room_occupants[room_path]
    del rooms_db[room_path]
    save_rooms()

    # Delete message file
    mp = room_messages_path(room_path)
    if os.path.exists(mp):
        os.remove(mp)

    await push_room_tree_to_all()
    await send_system(ws, f"🗑️ Room deleted.")


COMMANDS = {
    "help": cmd_help,
    "users": cmd_users,
    "kick": cmd_kick,
    "announce": cmd_announce,
    "makemod": cmd_makemod,
    "makeadmin": cmd_makeadmin,
    "demote": cmd_demote,
    "delete": cmd_delete,
}

# ─────────────────────────────────────────────
# Connection Handler
# ─────────────────────────────────────────────

async def handle_client(ws):
    client = None
    try:
        # ── Auth ──
        raw = await asyncio.wait_for(ws.recv(), timeout=30)
        data = json.loads(raw)

        msg_type = data.get("type")

        if msg_type == "register":
            username = data.get("username", "").strip()
            password = data.get("password", "").strip()
            if not username or len(username) > 20:
                await send(ws, {"type": "error", "content": "Invalid username (1-20 chars)."})
                return
            if not password or len(password) < 4:
                await send(ws, {"type": "error", "content": "Password must be at least 4 characters."})
                return
            if username.lower() in users_db:
                await send(ws, {"type": "error", "content": f"Username '{username}' is already registered. Please login."})
                return
            # Register
            users_db[username.lower()] = {
                "username": username,
                "password_hash": hash_password(password)
            }
            save_users()
            logger.info(f"Registered: {username}")

        elif msg_type == "login":
            username = data.get("username", "").strip()
            password = data.get("password", "").strip()
            if not username:
                await send(ws, {"type": "error", "content": "Please enter a username."})
                return
            ulow = username.lower()
            if ulow not in users_db:
                await send(ws, {"type": "error", "content": f"Username '{username}' not found. Please register."})
                return
            if users_db[ulow]["password_hash"] != hash_password(password):
                await send(ws, {"type": "error", "content": "Incorrect password."})
                return
            # Use the stored casing
            username = users_db[ulow]["username"]

        else:
            await send(ws, {"type": "error", "content": "First message must be register or login."})
            return

        # Check single session
        if username.lower() in username_map:
            await send(ws, {"type": "error", "content": f"'{username}' is already logged in from another browser. Please log out there first."})
            return

        # Set up client
        client = {"username": username, "room": None}
        clients[ws] = client
        username_map[username.lower()] = ws

        logger.info(f"Connected: {username}")

        # Send welcome
        await send(ws, {
            "type": "welcome",
            "username": username,
            "ts": now_ts()
        })

        # Send room tree
        await push_room_tree(ws)

        # ── Message Loop ──
        async for raw_msg in ws:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await send_system(ws, "❌ Invalid message.")
                continue

            mt = msg.get("type")

            if mt == "command":
                content = msg.get("content", "").strip()
                if not content.startswith("/"):
                    await send_system(ws, "❌ Commands must start with /")
                    continue
                parts = content[1:].split()
                cmd_name = parts[0].lower() if parts else ""
                cmd_args = parts[1:]
                handler = COMMANDS.get(cmd_name)
                if handler:
                    await handler(ws, cmd_args)
                else:
                    await send_system(ws, f"❌ Unknown command '/{cmd_name}'. Type /help.")

            elif mt == "message":
                content = msg.get("content", "").strip()
                room_path = client["room"]
                if not room_path:
                    await send_system(ws, "❌ You are not in any room.")
                    continue
                if not content:
                    continue

                chat_msg = {
                    "type": "message",
                    "from": client["username"],
                    "role": get_user_role_in_room(client["username"], room_path) or "member",
                    "room": room_path,
                    "content": content,
                    "ts": now_ts()
                }
                await broadcast_room(room_path, chat_msg)
                append_room_message(room_path, {**chat_msg, "full_ts": now_full()})

            elif mt == "image_message":
                room_path = client["room"]
                if not room_path:
                    await send_system(ws, "❌ You are not in any room.")
                    continue
                image_data = msg.get("image_data", "")
                filename = msg.get("filename", "image.png")
                caption = msg.get("caption", "").strip()

                if not image_data:
                    continue

                # Save image to uploads
                ext = os.path.splitext(filename)[1] or ".png"
                saved_name = f"{int(time.time()*1000)}_{secrets.token_hex(4)}{ext}"
                saved_path = os.path.join(UPLOADS_DIR, saved_name)
                
                # image_data is base64
                import base64
                try:
                    img_bytes = base64.b64decode(image_data.split(",")[-1] if "," in image_data else image_data)
                    with open(saved_path, "wb") as f:
                        f.write(img_bytes)
                except Exception:
                    await send_system(ws, "❌ Failed to process image.")
                    continue

                img_msg = {
                    "type": "image_message",
                    "from": client["username"],
                    "role": get_user_role_in_room(client["username"], room_path) or "member",
                    "room": room_path,
                    "image_url": f"/data/uploads/{saved_name}",
                    "filename": filename,
                    "caption": caption,
                    "ts": now_ts()
                }
                await broadcast_room(room_path, img_msg)
                append_room_message(room_path, {**img_msg, "full_ts": now_full()})

            elif mt == "create_room":
                room_name = msg.get("name", "").strip()
                parent = msg.get("parent", None)

                if not room_name or len(room_name) > 30:
                    await send_system(ws, "❌ Room name must be 1-30 characters.")
                    continue

                # Build room path
                safe_name = room_name.lower().replace(" ", "-")
                room_path_new = f"{parent}/{safe_name}" if parent else safe_name

                if room_path_new in rooms_db:
                    await send(ws, {"type": "error", "content": f"Room '{room_name}' already exists."})
                    continue

                # If sub-room, check parent exists and user is admin there
                if parent:
                    if parent not in rooms_db:
                        await send(ws, {"type": "error", "content": "Parent room not found."})
                        continue
                    parent_role = get_user_role_in_room(client["username"], parent)
                    if ROLE_POWER.get(parent_role, 0) < 3:
                        await send(ws, {"type": "error", "content": "Only room admins can create sub-rooms."})
                        continue

                code = generate_room_code()
                rooms_db[room_path_new] = {
                    "name": room_name,
                    "code": code,
                    "creator": client["username"],
                    "admins": [client["username"]],
                    "moderators": [],
                    "members": [],
                    "pending": [],
                    "parent": parent
                }
                save_rooms()
                logger.info(f"Room created: {room_path_new} by {client['username']} (code: {code})")

                await send(ws, {
                    "type": "room_created",
                    "room_path": room_path_new,
                    "room_name": room_name,
                    "code": code,
                    "ts": now_ts()
                })

                # Auto-join the creator
                await enter_room(ws, room_path_new)

            elif mt == "join_room_request":
                code = msg.get("code", "").strip().upper()
                if not code:
                    await send(ws, {"type": "error", "content": "Please enter a room code."})
                    continue

                # Find room by code
                target_room = None
                target_path = None
                for rp, rd in rooms_db.items():
                    if rd["code"] == code:
                        target_room = rd
                        target_path = rp
                        break

                if not target_room:
                    await send(ws, {"type": "error", "content": "Invalid room code."})
                    continue

                uname = client["username"]
                ulow = uname.lower()

                # Check if already a member
                all_members = ([a.lower() for a in target_room["admins"]] +
                               [m.lower() for m in target_room["moderators"]] +
                               [m.lower() for m in target_room["members"]])
                if ulow in all_members:
                    # Already a member, just join
                    await enter_room(ws, target_path)
                    continue

                # Check if already pending
                if ulow in [p.lower() for p in target_room.get("pending", [])]:
                    await send(ws, {"type": "join_pending", "room": target_path, "room_name": target_room["name"], "ts": now_ts()})
                    continue

                # Add to pending
                target_room.setdefault("pending", []).append(uname)
                save_rooms()

                await send(ws, {
                    "type": "join_pending",
                    "room": target_path,
                    "room_name": target_room["name"],
                    "ts": now_ts()
                })

                # Notify admins
                await push_pending_to_admins(target_path)
                # Also notify admins via system message
                for admin_name in target_room["admins"]:
                    aws = get_ws_by_name(admin_name)
                    if aws and aws in clients:
                        await send_system(aws, f"🔔 <b>{uname}</b> is requesting to join <b>{target_room['name']}</b>.")

            elif mt == "approve_join":
                room_path = msg.get("room", "")
                target_name = msg.get("username", "")
                room = rooms_db.get(room_path)
                if not room:
                    continue
                my_role = get_user_role_in_room(client["username"], room_path)
                if ROLE_POWER.get(my_role, 0) < 3:
                    await send_system(ws, "❌ Only admins can approve join requests.")
                    continue

                tlow = target_name.lower()
                room["pending"] = [p for p in room.get("pending", []) if p.lower() != tlow]
                # Find original name
                orig = target_name
                room["members"].append(orig)
                save_rooms()

                await send_system(ws, f"✅ {orig} has been approved to join.")
                await push_pending_list(ws, room_path)

                # Notify the user
                target_ws = get_ws_by_name(target_name)
                if target_ws and target_ws in clients:
                    await send(target_ws, {
                        "type": "join_approved",
                        "room": room_path,
                        "room_name": room["name"],
                        "ts": now_ts()
                    })
                    # Auto-enter them if they have no room
                    if clients[target_ws]["room"] is None:
                        await enter_room(target_ws, room_path)

                await push_room_tree_to_all()

            elif mt == "reject_join":
                room_path = msg.get("room", "")
                target_name = msg.get("username", "")
                room = rooms_db.get(room_path)
                if not room:
                    continue
                my_role = get_user_role_in_room(client["username"], room_path)
                if ROLE_POWER.get(my_role, 0) < 3:
                    await send_system(ws, "❌ Only admins can reject join requests.")
                    continue

                tlow = target_name.lower()
                room["pending"] = [p for p in room.get("pending", []) if p.lower() != tlow]
                save_rooms()

                await send_system(ws, f"❌ {target_name}'s join request has been rejected.")
                await push_pending_list(ws, room_path)

                target_ws = get_ws_by_name(target_name)
                if target_ws and target_ws in clients:
                    await send(target_ws, {
                        "type": "join_rejected",
                        "room": room_path,
                        "room_name": room["name"],
                        "ts": now_ts()
                    })

            elif mt == "enter_room":
                # Client clicks a room they are already a member of
                room_path = msg.get("room", "")
                room = rooms_db.get(room_path)
                if not room:
                    await send(ws, {"type": "error", "content": "Room not found."})
                    continue
                role = get_user_role_in_room(client["username"], room_path)
                if not role:
                    await send(ws, {"type": "error", "content": "You are not a member of this room."})
                    continue
                await enter_room(ws, room_path)

            elif mt == "logout":
                await send(ws, {"type": "logged_out"})
                break

            else:
                await send_system(ws, "❌ Unknown message type.")

    except asyncio.TimeoutError:
        logger.info("Client timed out during auth.")
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except websockets.exceptions.ConnectionClosedError as e:
        logger.warning(f"Connection closed with error: {e}")
    except Exception as e:
        logger.error(f"Unhandled error: {e}", exc_info=True)
    finally:
        if ws in clients and client:
            username = client["username"]
            room_path = client["room"]
            username_map.pop(username.lower(), None)
            del clients[ws]

            if room_path and room_path in room_occupants:
                room_occupants[room_path].discard(ws)
                await broadcast_system_room(room_path, f"🔴 {username} disconnected.")
                await push_user_list(room_path)
                if not room_occupants[room_path]:
                    del room_occupants[room_path]

            await push_room_tree_to_all()
            logger.info(f"Disconnected: {username}")


# ─────────────────────────────────────────────
# HTTP Static File Server (websockets 16.0 API)
# ─────────────────────────────────────────────

STATIC_FILES = {
    "/": "index_v2.html",
    "/index_v2.html": "index_v2.html",
    "/chat_v2.html": "chat_v2.html",
    "/style_v2.css": "style_v2.css",
    "/index.html": "index.html",
    "/chat.html": "chat.html",
    "/style.css": "style.css",
}

def get_content_type(filepath):
    ct, _ = mimetypes.guess_type(filepath)
    return ct or "application/octet-stream"

def process_request(connection, request):
    """Intercept HTTP requests to serve static files.
    Return None to let WebSocket upgrade proceed normally."""
    # WebSocket upgrade — let it through
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None

    path = request.path

    # Serve uploaded images: /data/uploads/<filename>
    if path.startswith("/data/uploads/"):
        filename = path.split("/")[-1]
        filepath = os.path.join(UPLOADS_DIR, filename)
        if os.path.isfile(filepath):
            ct = get_content_type(filepath)
            with open(filepath, "rb") as f:
                body = f.read()
            return Response(
                200, "OK",
                websockets.datastructures.Headers([
                    ("Content-Type", ct),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "public, max-age=3600"),
                ]),
                body,
            )
        return Response(
            404, "Not Found",
            websockets.datastructures.Headers([("Content-Type", "text/plain")]),
            b"File not found",
        )

    # Serve known static files
    if path in STATIC_FILES:
        filepath = os.path.join(BASE_DIR, STATIC_FILES[path])
        if os.path.isfile(filepath):
            ct = get_content_type(filepath)
            with open(filepath, "rb") as f:
                body = f.read()
            return Response(
                200, "OK",
                websockets.datastructures.Headers([
                    ("Content-Type", ct),
                    ("Content-Length", str(len(body))),
                ]),
                body,
            )

    # Favicon
    if path == "/favicon.ico":
        return Response(
            404, "Not Found",
            websockets.datastructures.Headers([("Content-Type", "text/plain")]),
            b"",
        )

    # Default: redirect to /
    return Response(
        301, "Moved Permanently",
        websockets.datastructures.Headers([("Location", "/")]),
        b"",
    )


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

async def main():
    ensure_dirs()
    global users_db, rooms_db
    users_db = load_json(USERS_FILE, {})
    rooms_db = load_json(ROOMS_FILE, {})
    logger.info(f"Loaded {len(users_db)} users, {len(rooms_db)} rooms")

    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8765))
    logger.info(f"🚀 Server starting on http://{host}:{port}")
    logger.info(f"   WebSocket: ws://{host}:{port}")
    logger.info(f"   HTTP:      http://{host}:{port}")
    async with websockets.serve(
        handle_client,
        host,
        port,
        max_size=10 * 1024 * 1024,
        process_request=process_request,
    ):
        logger.info("✅ Server running. Press Ctrl+C to stop.")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
