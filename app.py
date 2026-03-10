import asyncio
import json
import os
from contextlib import asynccontextmanager

import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pywebpush import webpush, WebPushException

load_dotenv()

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_MAILTO      = os.environ.get("VAPID_MAILTO", "mailto:admin@example.com")
DB_PATH           = os.environ.get("DB_PATH", "love_ping.db")

SW_SCRIPT = r"""
self.addEventListener('push', function(event) {
    const data = event.data ? event.data.json() : {};
    event.waitUntil(
        self.registration.showNotification(data.title || '\u{1F497}', {
            body: data.body || 'thinking of you~',
            vibrate: [200, 100, 200],
            tag: 'love-ping',
            renotify: true
        })
    );
});
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(clients.openWindow('/'));
});
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id    TEXT PRIMARY KEY,
                pin        TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS presence (
                room_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (room_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pings (
                room_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                room_id      TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                subscription TEXT NOT NULL,
                updated_at   TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (room_id, user_id)
            )
        """)
        await db.commit()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/sw.js")
async def service_worker():
    return Response(
        content=SW_SCRIPT,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"}
    )


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/vapid-public-key")
async def vapid_public_key():
    return Response(content=VAPID_PUBLIC_KEY, media_type="text/plain")


# ── PIN helpers ───────────────────────────────────────────────────────────────

async def _check_pin(db, room_id: str, pin: str):
    async with db.execute(
        "SELECT pin FROM rooms WHERE room_id = ?", (room_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if not row or row[0] != pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")


class SetPinBody(BaseModel):
    room_id: str
    pin: str


class VerifyPinBody(BaseModel):
    room_id: str
    pin: str


@app.get("/api/room/{room_id}/has-pin")
async def has_pin(room_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM rooms WHERE room_id = ?", (room_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return {"has_pin": row is not None}


@app.post("/api/room/set-pin")
async def set_pin(body: SetPinBody):
    if not body.pin.isdigit() or len(body.pin) != 4:
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM rooms WHERE room_id = ?", (body.room_id,)
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="PIN already set for this room")
        await db.execute(
            "INSERT INTO rooms (room_id, pin) VALUES (?, ?)",
            (body.room_id, body.pin)
        )
        await db.commit()
    return {"ok": True}


@app.post("/api/room/verify-pin")
async def verify_pin(body: VerifyPinBody):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pin FROM rooms WHERE room_id = ?", (body.room_id,)
        ) as cursor:
            row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Room not found")
    if row[0] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")
    return {"ok": True}


# ── Room join (presence) ──────────────────────────────────────────────────────

class JoinBody(BaseModel):
    room_id: str
    user_id: str
    pin: str


@app.post("/api/room/join")
async def join_room(body: JoinBody):
    if body.user_id not in ("a", "b"):
        raise HTTPException(status_code=400, detail="user_id must be 'a' or 'b'")
    async with aiosqlite.connect(DB_PATH) as db:
        await _check_pin(db, body.room_id, body.pin)
        await db.execute(
            "INSERT OR IGNORE INTO presence (room_id, user_id) VALUES (?, ?)",
            (body.room_id, body.user_id)
        )
        await db.commit()
    return {"ok": True}


# ── Room status ───────────────────────────────────────────────────────────────

@app.get("/api/room/{room_id}")
async def room_status(room_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM presence WHERE room_id = ?", (room_id,)
        ) as cursor:
            presence_rows = await cursor.fetchall()
        async with db.execute(
            "SELECT user_id, count FROM pings WHERE room_id = ?", (room_id,)
        ) as cursor:
            ping_rows = await cursor.fetchall()
    present = [r[0] for r in presence_rows]
    taps = {r[0]: r[1] for r in ping_rows}
    return {"count": len(present), "slots": present, "taps": taps}


# ── Subscribe ─────────────────────────────────────────────────────────────────

class SubscribeBody(BaseModel):
    room_id: str
    user_id: str
    pin: str
    subscription: dict


@app.post("/api/subscribe")
async def subscribe(body: SubscribeBody):
    if body.user_id not in ("a", "b"):
        raise HTTPException(status_code=400, detail="user_id must be 'a' or 'b'")
    async with aiosqlite.connect(DB_PATH) as db:
        await _check_pin(db, body.room_id, body.pin)
        await db.execute(
            """INSERT OR REPLACE INTO subscriptions (room_id, user_id, subscription, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (body.room_id, body.user_id, json.dumps(body.subscription))
        )
        await db.commit()
    return {"ok": True}


# ── Ping ──────────────────────────────────────────────────────────────────────

class PingBody(BaseModel):
    room_id: str
    user_id: str
    pin: str


def _send_push_sync(subscription_info: dict, payload: dict):
    webpush(
        subscription_info=subscription_info,
        data=json.dumps(payload),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={"sub": VAPID_MAILTO},
        content_encoding="aes128gcm",
    )


@app.post("/api/ping")
async def ping(body: PingBody):
    partner = "b" if body.user_id == "a" else "a"
    async with aiosqlite.connect(DB_PATH) as db:
        await _check_pin(db, body.room_id, body.pin)
        # Increment sender's tap count
        await db.execute(
            """INSERT INTO pings (room_id, user_id, count) VALUES (?, ?, 1)
               ON CONFLICT(room_id, user_id) DO UPDATE SET count = count + 1""",
            (body.room_id, body.user_id)
        )
        await db.commit()
        # Get partner's push subscription
        async with db.execute(
            "SELECT subscription FROM subscriptions WHERE room_id = ? AND user_id = ?",
            (body.room_id, partner)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return {"ok": True, "delivered": False, "reason": "partner not subscribed yet"}

    sub_info = json.loads(row[0])
    payload  = {"title": "💗", "body": "thinking of you~"}

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_push_sync, sub_info, payload)
        return {"ok": True, "delivered": True}
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM subscriptions WHERE room_id = ? AND user_id = ?",
                    (body.room_id, partner)
                )
                await db.commit()
        return {"ok": True, "delivered": False, "reason": str(e)}
