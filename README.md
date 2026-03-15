# 💬 ReCode Comms v2 — Internal Messaging Platform

A real-time, room-based messaging platform with role management, image sharing, and persistent storage. Built with **Python WebSockets** and **vanilla HTML/CSS/JS** — no frameworks, no npm.

---

## ✨ Features

| Feature | Description |
|---|---|
| 🔐 **Password Auth** | Register/login with username + password (SHA-256 hashed) |
| 🏠 **Room System** | Create rooms, get a 6-char invite code, share it with others |
| ✅ **Join Approval** | Admin approves or rejects join requests — no one enters uninvited |
| 👑 **Room-Based Roles** | Room creator = admin. Admin can promote moderators and more admins |
| 💾 **Persistent Storage** | Users, rooms, roles, and chat history saved to JSON files |
| 🖼️ **Image Sharing** | Share images inline with captions, click to view full-size |
| 📁 **Sub-Rooms** | Create rooms inside rooms for topic-based discussions |
| 🔒 **Single Session** | One browser per user — prevents duplicate logins |
| 🎨 **Minecraft Pixel Art UI** | 8-bit pixel art theme with Press Start 2P font, pastel coffee & pink palette, 3D block buttons, and hard dark borders |

---

## 📁 Project Structure

```
ReCode/
├── server_v2.py       # WebSocket server (auth, rooms, persistence, images)
├── index_v2.html      # Login / Register page
├── chat_v2.html       # Main chat interface
├── style_v2.css       # pixel art theme (pastel coffee & pink)
├── requirements.txt   # Python dependencies
├── data/              # Auto-created on first run
│   ├── users.json     # Registered accounts (hashed passwords)
│   ├── rooms.json     # Room configs, roles, pending requests
│   ├── messages/      # Per-room chat history
│   └── uploads/       # Shared images
│
├── server.py          # (Original v1 — unused)
├── index.html         # (Original v1 — unused)
├── chat.html          # (Original v1 — unused)
└── style.css          # (Original v1 — unused)
```

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.8+**
- **Git**
- A modern browser (Chrome, Firefox, Edge)

### 1. Clone the repository

```bash
git clone https://github.com/your-username/ReCode.git
cd ReCode
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the WebSocket server

```bash
python server_v2.py
```

You should see:

```
🚀 Server starting on ws://0.0.0.0:8765
✅ Server running. Press Ctrl+C to stop.
```

### 4. Start the HTTP server

Open a **second terminal** in the same folder:

```bash
python -m http.server 3000
```

### 5. Open the app

Go to **http://localhost:3000/index_v2.html** in your browser.

> ⚠️ **Important:** Make sure the old `server.py` is NOT running, or it will steal port 8765.

---


## 👑 Role System

Roles are **per-room**, not global. Each room has its own admin, moderators, and members.

| Role | How to get it | Permissions |
|---|---|---|
| **Admin** 👑 | Create the room, or get promoted by another admin | Full control — approve joins, promote/demote, kick, delete room, create sub-rooms |
| **Moderator** 🛡️ | Promoted by an admin via `/makemod` | Kick members, make announcements |
| **Member** 👤 | Join via invite code + admin approval | Send messages, share images |

> 💡 Admin status **persists** across logouts and server restarts (stored in `data/rooms.json`).

---

## 🏠 Room Workflow

```
1. Alice registers and clicks "+ Create Room"
2. She names it "dev-team" → gets code: A1B2C3
3. Alice shares the code with Bob

4. Bob registers, clicks "+ Join Room", enters A1B2C3
5. Alice sees Bob's pending request → clicks ✓ Approve
6. Bob enters the room and can start chatting

7. Alice types: /makemod bob → Bob becomes a moderator
8. Alice clicks "+ Add Sub-Room" → creates "frontend" under "dev-team"
```

---

## ⚡ Slash Commands

Type commands in the message input and press Enter.

| Command | Who can use | Description |
|---|---|---|
| `/help` | Everyone | List commands available to your role |
| `/users` | Everyone | List users in current room |
| `/kick <user>` | Mod+ | Remove a user from the room |
| `/announce <msg>` | Mod+ | Send a highlighted announcement |
| `/makemod <user>` | Admin | Promote member to moderator |
| `/makeadmin <user>` | Admin | Promote member to admin |
| `/demote <user>` | Admin | Demote admin/mod to member |
| `/delete` | Admin | Delete the current room |

---

## 🖼️ Image Sharing

1. Click the 📎 button next to the message input
2. Select an image (max 5MB)
3. Optionally add a caption in the text field
4. Press Send
5. Images display inline — click to view full-size in a lightbox

Images are saved to `data/uploads/` and persist across sessions.

---

## 📁 Sub-Rooms

Rooms can contain child rooms for organizing discussions by topic:

```
# dev-team          ← main room
  # frontend        ← sub-room
  # backend         ← sub-room
  # devops          ← sub-room
```

- Only **room admins** can create sub-rooms
- Sub-rooms appear as a tree in the left sidebar
- Each sub-room has its own members, chat history, and roles

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| "Already logged in from another browser" | Log out from the other browser, or close it and wait ~15 seconds |
| Can't connect (WebSocket error) | Make sure `server_v2.py` is running and port 8765 is free |
| Old server stealing the port | Kill the old `server.py` process: `taskkill /F /PID <pid>` or stop it with Ctrl+C |
| "First message must be a register event" | You're accidentally connecting to the old `server.py` — kill it |
| Page not loading | Make sure `python -m http.server 3000` is running |
| Images not displaying | Check that `data/uploads/` exists and the HTTP server is serving from the right directory |

---

## 🏗️ Architecture

```
Browser ──── ws://0.0.0.0:8765 ────► server_v2.py
               (WebSocket)               │
                                          ├── users_db     → data/users.json
                                          ├── rooms_db     → data/rooms.json
                                          ├── messages     → data/messages/*.json
                                          └── uploads      → data/uploads/*
```

- **Persistence**: All data saved to JSON files in the `data/` directory
- **Auth**: SHA-256 password hashing, single-session enforcement
- **Real-time**: Messages broadcast via `asyncio.gather()` to all room occupants
- **Images**: Stored as files on disk, served by the HTTP server

---

## 📦 Dependencies

```
websockets >= 12.0    # Python WebSocket server
```

Frontend uses **zero npm packages** — pure HTML, CSS, and vanilla JavaScript.

---

## 📄 License

Internal use. Feel free to modify and extend.
