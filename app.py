import asyncio
import json
import os
from contextlib import asynccontextmanager

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

# ── In-memory state ───────────────────────────────────────────────────────────
# rooms[room_id] = pin
rooms: dict[str, str] = {}
# presence[room_id][session_id] = user_id ('a' or 'b')
presence: dict[str, dict[str, str]] = {}
# pings[room_id][user_id] = count
pings: dict[str, dict[str, int]] = {}
# subscriptions[room_id][user_id] = push subscription dict
subscriptions: dict[str, dict[str, dict]] = {}


@asynccontextmanager
async def lifespan(app):
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


# ── PIN ───────────────────────────────────────────────────────────────────────

class SetPinBody(BaseModel):
    room_id: str
    pin: str


class VerifyPinBody(BaseModel):
    room_id: str
    pin: str


@app.get("/api/room/{room_id}/has-pin")
async def has_pin(room_id: str):
    return {"has_pin": room_id in rooms}


@app.post("/api/room/set-pin")
async def set_pin(body: SetPinBody):
    if not body.pin.isdigit() or len(body.pin) != 4:
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")
    if body.room_id in rooms:
        raise HTTPException(status_code=409, detail="PIN already set for this room")
    rooms[body.room_id] = body.pin
    return {"ok": True}


@app.post("/api/room/verify-pin")
async def verify_pin(body: VerifyPinBody):
    if body.room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    if rooms[body.room_id] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")
    return {"ok": True}


# ── Join ──────────────────────────────────────────────────────────────────────

class JoinBody(BaseModel):
    room_id: str
    pin: str
    session_id: str


@app.post("/api/room/join")
async def join_room(body: JoinBody):
    if body.room_id not in rooms or rooms[body.room_id] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")

    room_presence = presence.setdefault(body.room_id, {})

    # Reuse existing slot for this device
    if body.session_id in room_presence:
        return {"ok": True, "user_id": room_presence[body.session_id]}

    # Assign next available slot
    taken = set(room_presence.values())
    if "a" not in taken:
        new_uid = "a"
    elif "b" not in taken:
        new_uid = "b"
    else:
        raise HTTPException(status_code=409, detail="Room is full")

    room_presence[body.session_id] = new_uid
    return {"ok": True, "user_id": new_uid}


# ── Room status ───────────────────────────────────────────────────────────────

@app.get("/api/room/{room_id}")
async def room_status(room_id: str):
    room_presence = presence.get(room_id, {})
    slots = list(set(room_presence.values()))
    room_pings = pings.get(room_id, {})
    return {"count": len(slots), "slots": slots, "taps": room_pings}


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
    if body.room_id not in rooms or rooms[body.room_id] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")
    subscriptions.setdefault(body.room_id, {})[body.user_id] = body.subscription
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
    if body.room_id not in rooms or rooms[body.room_id] != body.pin:
        raise HTTPException(status_code=403, detail="Wrong PIN")

    # Increment tap count
    room_pings = pings.setdefault(body.room_id, {})
    room_pings[body.user_id] = room_pings.get(body.user_id, 0) + 1

    # Send push to partner
    partner = "b" if body.user_id == "a" else "a"
    sub = subscriptions.get(body.room_id, {}).get(partner)
    if not sub:
        return {"ok": True, "delivered": False, "reason": "partner not subscribed yet"}

    payload = {"title": "💗", "body": "thinking of you~"}
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _send_push_sync, sub, payload)
        return {"ok": True, "delivered": True}
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            subscriptions.get(body.room_id, {}).pop(partner, None)
        return {"ok": True, "delivered": False, "reason": str(e)}
