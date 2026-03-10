import asyncio
import json
import os
from contextlib import asynccontextmanager

import asyncpg
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
DATABASE_URL      = os.environ.get("DATABASE_URL", "")

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

db_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global db_pool
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
    return db_pool


async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                pin     TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS presence (
                room_id    TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_id    TEXT NOT NULL,
                PRIMARY KEY (room_id, session_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pings (
                room_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                count   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                room_id      TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                subscription TEXT NOT NULL,
                PRIMARY KEY (room_id, user_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         SERIAL PRIMARY KEY,
                room_id    TEXT NOT NULL,
                sender_id  TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)


@asynccontextmanager
async def lifespan(app):
    pool = await get_pool()
    await init_db(pool)
    yield
    await pool.close()


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


# ── PIN ───────────────────────────────────────────────────────────────────────

class SetPinBody(BaseModel):
    room_id: str
    pin: str


class VerifyPinBody(BaseModel):
    room_id: str
    pin: str


@app.get("/api/room/{room_id}/has-pin")
async def has_pin(room_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM rooms WHERE room_id=$1", room_id)
    return {"has_pin": row is not None}


@app.post("/api/room/set-pin")
async def set_pin(body: SetPinBody):
    if not body.pin.isdigit() or len(body.pin) != 4:
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT 1 FROM rooms WHERE room_id=$1", body.room_id)
        if existing:
            raise HTTPException(status_code=409, detail="PIN already set for this room")
        await conn.execute("INSERT INTO rooms(room_id, pin) VALUES($1, $2)", body.room_id, body.pin)
    return {"ok": True}


@app.post("/api/room/verify-pin")
async def verify_pin(body: VerifyPinBody):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
    if not row:
        raise HTTPException(status_code=404, detail="Room not found")
    if row["pin"] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")
    return {"ok": True}


# ── Join ──────────────────────────────────────────────────────────────────────

class JoinBody(BaseModel):
    room_id: str
    pin: str
    session_id: str


@app.post("/api/room/join")
async def join_room(body: JoinBody):
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
        if not room or room["pin"] != body.pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")

        existing = await conn.fetchrow(
            "SELECT user_id FROM presence WHERE room_id=$1 AND session_id=$2",
            body.room_id, body.session_id
        )
        if existing:
            return {"ok": True, "user_id": existing["user_id"]}

        taken = await conn.fetch(
            "SELECT user_id FROM presence WHERE room_id=$1", body.room_id
        )
        taken_ids = {r["user_id"] for r in taken}
        if "a" not in taken_ids:
            new_uid = "a"
        elif "b" not in taken_ids:
            new_uid = "b"
        else:
            raise HTTPException(status_code=409, detail="Room is full")

        await conn.execute(
            "INSERT INTO presence(room_id, session_id, user_id) VALUES($1, $2, $3)",
            body.room_id, body.session_id, new_uid
        )
    return {"ok": True, "user_id": new_uid}


# ── Room status ───────────────────────────────────────────────────────────────

@app.get("/api/room/{room_id}")
async def room_status(room_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM presence WHERE room_id=$1", room_id)
        slots = list({r["user_id"] for r in rows})
        ping_rows = await conn.fetch("SELECT user_id, count FROM pings WHERE room_id=$1", room_id)
        taps = {r["user_id"]: r["count"] for r in ping_rows}
    return {"count": len(slots), "slots": slots, "taps": taps}


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
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
        if not room or room["pin"] != body.pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")
        await conn.execute(
            """INSERT INTO subscriptions(room_id, user_id, subscription)
               VALUES($1, $2, $3)
               ON CONFLICT(room_id, user_id) DO UPDATE SET subscription=EXCLUDED.subscription""",
            body.room_id, body.user_id, json.dumps(body.subscription)
        )
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


# ── Messages ──────────────────────────────────────────────────────────────────

class MessageBody(BaseModel):
    room_id: str
    user_id: str
    pin: str
    content: str


@app.post("/api/message")
async def send_message(body: MessageBody):
    if body.user_id not in ("a", "b"):
        raise HTTPException(status_code=400, detail="invalid user_id")
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="empty message")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="message too long")

    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
        if not room or room["pin"] != body.pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")
        tap_row = await conn.fetchrow(
            "SELECT count FROM pings WHERE room_id=$1 AND user_id=$2",
            body.room_id, body.user_id
        )
        allowed = (tap_row["count"] // 100) if tap_row else 0
        sent_count = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE room_id=$1 AND sender_id=$2",
            body.room_id, body.user_id
        )
        if sent_count >= allowed:
            raise HTTPException(status_code=409, detail="Need 100 more hearts to send another message")
        await conn.execute(
            "INSERT INTO messages(room_id, sender_id, content) VALUES($1, $2, $3)",
            body.room_id, body.user_id, content
        )
        partner = "b" if body.user_id == "a" else "a"
        sub_row = await conn.fetchrow(
            "SELECT subscription FROM subscriptions WHERE room_id=$1 AND user_id=$2",
            body.room_id, partner
        )

    if sub_row:
        sub = json.loads(sub_row["subscription"])
        payload = {"title": "💌 message", "body": content[:80]}
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _send_push_sync, sub, payload)
        except WebPushException:
            pass

    return {"ok": True}


@app.get("/api/room/{room_id}/messages")
async def get_messages(room_id: str, pin: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", room_id)
        if not room or room["pin"] != pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")
        rows = await conn.fetch(
            "SELECT sender_id, content, created_at FROM messages WHERE room_id=$1 ORDER BY created_at ASC",
            room_id
        )
    return {"messages": [
        {"sender": r["sender_id"], "content": r["content"], "ts": r["created_at"].isoformat()}
        for r in rows
    ]}


# ── Angry ─────────────────────────────────────────────────────────────────────

class AngryBody(BaseModel):
    room_id: str
    user_id: str
    pin: str
    amount: int


@app.post("/api/angry")
async def angry(body: AngryBody):
    if body.user_id not in ("a", "b"):
        raise HTTPException(status_code=400, detail="invalid user_id")
    if body.amount not in range(5, 55, 5):
        raise HTTPException(status_code=400, detail="amount must be 5-50 in steps of 5")

    partner = "b" if body.user_id == "a" else "a"
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
        if not room or room["pin"] != body.pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")
        await conn.execute(
            """UPDATE pings SET count = GREATEST(count - $1, 0)
               WHERE room_id=$2 AND user_id=$3""",
            body.amount, body.room_id, partner
        )
        sub_row = await conn.fetchrow(
            "SELECT subscription FROM subscriptions WHERE room_id=$1 AND user_id=$2",
            body.room_id, partner
        )

    if sub_row:
        sub = json.loads(sub_row["subscription"])
        payload = {"title": "💢 they're upset", "body": f"{body.amount} 💗 taken away"}
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _send_push_sync, sub, payload)
        except WebPushException:
            pass

    return {"ok": True}


@app.post("/api/ping")
async def ping(body: PingBody):
    pool = await get_pool()
    async with pool.acquire() as conn:
        room = await conn.fetchrow("SELECT pin FROM rooms WHERE room_id=$1", body.room_id)
        if not room or room["pin"] != body.pin:
            raise HTTPException(status_code=403, detail="Wrong PIN")

        await conn.execute(
            """INSERT INTO pings(room_id, user_id, count) VALUES($1, $2, 1)
               ON CONFLICT(room_id, user_id) DO UPDATE SET count = pings.count + 1""",
            body.room_id, body.user_id
        )

        partner = "b" if body.user_id == "a" else "a"
        sub_row = await conn.fetchrow(
            "SELECT subscription FROM subscriptions WHERE room_id=$1 AND user_id=$2",
            body.room_id, partner
        )

    if not sub_row:
        return {"ok": True, "delivered": False, "reason": "partner not subscribed yet"}

    sub = json.loads(sub_row["subscription"])
    payload = {"title": "💗", "body": "thinking of you~"}
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_push_sync, sub, payload)
        return {"ok": True, "delivered": True}
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            pool2 = await get_pool()
            async with pool2.acquire() as conn2:
                await conn2.execute(
                    "DELETE FROM subscriptions WHERE room_id=$1 AND user_id=$2",
                    body.room_id, partner
                )
        return {"ok": True, "delivered": False, "reason": str(e)}
