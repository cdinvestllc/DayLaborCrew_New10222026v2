import asyncio
import os
import uuid
from datetime import datetime, timezone

import requests
from fastapi import HTTPException, UploadFile

from database import db

STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = "thedaylaborers"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
FILE_URL_PREFIX = "/api/public/files/"
_storage_key = None

# Detected from file content, never trusted from the client filename/content-type
_SIGNATURES = [
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"\x00\x00\x01\x00", "ico", "image/x-icon"),
]


def _init_storage(force: bool = False) -> str:
    global _storage_key
    if _storage_key and not force:
        return _storage_key
    resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": os.environ["EMERGENT_LLM_KEY"]}, timeout=30)
    resp.raise_for_status()
    _storage_key = resp.json()["storage_key"]
    return _storage_key


def init_storage():
    return _init_storage()


def _put(path: str, data: bytes, content_type: str) -> dict:
    for attempt in range(2):
        key = _init_storage(force=attempt > 0)
        resp = requests.put(f"{STORAGE_URL}/objects/{path}",
                            headers={"X-Storage-Key": key, "Content-Type": content_type}, data=data, timeout=120)
        if resp.status_code != 404:
            break
    resp.raise_for_status()
    return resp.json()


def _get(path: str) -> bytes:
    for attempt in range(2):
        key = _init_storage(force=attempt > 0)
        resp = requests.get(f"{STORAGE_URL}/objects/{path}", headers={"X-Storage-Key": key}, timeout=60)
        if resp.status_code != 404:
            break
    resp.raise_for_status()
    return resp.content


def detect_image(data: bytes) -> tuple[str, str] | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    for sig, ext, mime in _SIGNATURES:
        if data.startswith(sig):
            return ext, mime
    return None


async def save_image(file: UploadFile, owner_id: str, category: str) -> str:
    """Validates and uploads an image; returns its public URL path."""
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller")
    detected = detect_image(data)
    if not detected:
        raise HTTPException(status_code=400, detail="File must be a JPG, PNG, GIF or WEBP image")
    ext, mime = detected
    path = f"{APP_NAME}/{category}/{owner_id}/{uuid.uuid4().hex}.{ext}"
    try:
        result = await asyncio.to_thread(_put, path, data, mime)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Image upload failed. Please try again.")
    stored_path = result.get("path", path)
    await db.files.insert_one({
        "id": str(uuid.uuid4()),
        "storage_path": stored_path,
        "owner_id": owner_id,
        "category": category,
        "content_type": mime,
        "size": len(data),
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return FILE_URL_PREFIX + stored_path


def detect_video(data: bytes) -> tuple[str, str] | None:
    """Detect supported video containers from bytes, not the supplied MIME type."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        # MP4/MOV ISO Base Media File Format; extension is .mp4 for broad client support.
        return "mp4", "video/mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm", "video/webm"
    return None


async def save_video(file: UploadFile, owner_id: str, category: str) -> str:
    """Validate and upload a small MP4/WebM file; duration is checked by the caller."""
    data = await file.read(MAX_VIDEO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Video file is empty")
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=400, detail="Video must be 50 MB or smaller")
    detected = detect_video(data)
    if not detected:
        raise HTTPException(status_code=400, detail="Video must be a valid MP4 or WebM file")
    ext, mime = detected
    path = f"{APP_NAME}/{category}/{owner_id}/{uuid.uuid4().hex}.{ext}"
    try:
        result = await asyncio.to_thread(_put, path, data, mime)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Video upload failed. Please try again.")
    stored_path = result.get("path", path)
    await db.files.insert_one({
        "id": str(uuid.uuid4()), "storage_path": stored_path, "owner_id": owner_id,
        "category": category, "content_type": mime, "size": len(data),
        "is_deleted": False, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return FILE_URL_PREFIX + stored_path


async def load_image(path: str) -> tuple[bytes, str]:
    record = await db.files.find_one({"storage_path": path, "is_deleted": False}, {"_id": 0})
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data = await asyncio.to_thread(_get, path)
    except requests.RequestException:
        raise HTTPException(status_code=404, detail="File not found")
    return data, record["content_type"]


async def _store_bytes(data: bytes, owner_id: str, category: str) -> str | None:
    detected = detect_image(data)
    if not detected or len(data) > MAX_IMAGE_BYTES:
        return None
    ext, mime = detected
    path = f"{APP_NAME}/{category}/{owner_id}/{uuid.uuid4().hex}.{ext}"
    result = await asyncio.to_thread(_put, path, data, mime)
    stored = result.get("path", path)
    await db.files.insert_one({
        "id": str(uuid.uuid4()), "storage_path": stored, "owner_id": owner_id, "category": category,
        "content_type": mime, "size": len(data), "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return FILE_URL_PREFIX + stored


async def migrate_legacy_uploads(uploads_root) -> int:
    """One-time move of pod-local /uploads/... images into object storage; idempotent."""
    async def convert(url, owner, category):
        if not isinstance(url, str) or not url.startswith("/uploads/"):
            return url
        local = uploads_root / url[len("/uploads/"):]
        if ".." in url or not local.is_file():
            return url
        return await _store_bytes(local.read_bytes(), owner, category) or url

    moved = 0
    query = {"$or": [{f: {"$regex": "^/uploads/"}} for f in ("profile_photo", "logo", "portfolio")]}
    async for user in db.users.find(query, {"_id": 0, "id": 1, "profile_photo": 1, "logo": 1, "portfolio": 1}):
        update = {}
        for field, cat in (("profile_photo", "profile_photos"), ("logo", "logos")):
            new = await convert(user.get(field), user["id"], cat)
            if new != user.get(field):
                update[field] = new
        portfolio = user.get("portfolio") or []
        new_portfolio = [await convert(u, user["id"], "portfolio") for u in portfolio]
        if new_portfolio != portfolio:
            update["portfolio"] = new_portfolio
        if update:
            await db.users.update_one({"id": user["id"]}, {"$set": update})
            moved += 1
    settings = await db.settings.find_one({}, {"_id": 0, "site_logo": 1, "site_favicon": 1}) or {}
    update = {}
    for field in ("site_logo", "site_favicon"):
        new = await convert(settings.get(field), "site", "cms")
        if new != settings.get(field):
            update[field] = new
    if update:
        await db.settings.update_one({}, {"$set": update})
    return moved
