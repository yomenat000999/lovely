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


class SubscribeBody(BaseModel):
    room_id: str
    user_id: str
    subscription: dict


class PingBody(BaseModel):
    room_id: str
    user_id: str


@app.post("/api/subscribe")
async def subscribe(body: SubscribeBody):
    if body.user_id not in ("a", "b"):
        raise HTTPException(status_code=400, detail="user_id must be 'a' or 'b'")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO subscriptions (room_id, user_id, subscription, updated_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (body.room_id, body.user_id, json.dumps(body.subscription))
        )
        await db.commit()
    return {"ok": True}


@app.get("/api/room/{room_id}")
async def room_status(room_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM subscriptions WHERE room_id = ?", (room_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    slots = [r[0] for r in rows]
    return {"count": len(slots), "slots": slots}


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
