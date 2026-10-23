import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict

from dotenv import load_dotenv
from fastapi import HTTPException, Depends, Request, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from slowapi import Limiter

load_dotenv(Path(__file__).parent / ".env")

from database import db  # noqa: E402

ALGORITHM = "HS256"
ISSUER = "thedaylaborers"
AUDIENCE = "thedaylaborers-api"
SESSION_COOKIE = "tdl_session"
CSRF_COOKIE = "tdl_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLES = {"crew", "contractor", "advertiser", "admin", "super_admin"}
_WEAK_SECRETS = {"thedaylaborers_secret", "secret", "changeme"}


def _load_secret() -> str:
    secret = os.environ.get("JWT_SECRET_KEY", "")
    if len(secret) < 32 or secret.lower() in _WEAK_SECRETS:
        raise RuntimeError("JWT_SECRET_KEY must be set to a random value of at least 32 characters")
    return secret


SECRET_KEY = _load_secret()
EXPIRE_HOURS = int(os.environ["JWT_EXPIRE_HOURS"])
if not 1 <= EXPIRE_HOURS <= 72:
    raise RuntimeError("JWT_EXPIRE_HOURS must be between 1 and 72")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)
security_logger = logging.getLogger("security")


TRUSTED_PROXY_HOPS = int(os.environ["TRUSTED_PROXY_HOPS"])


def client_ip(request: Request) -> str:
    """Client IP as seen by the outermost trusted proxy; client-supplied XFF entries are ignored."""
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(parts) > TRUSTED_PROXY_HOPS:
        return parts[-(TRUSTED_PROXY_HOPS + 1)]
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=client_ip, default_limits=["120/minute"])


def log_security_event(event: str, request: Optional[Request] = None, **fields):
    ip = client_ip(request) if request else "-"
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    security_logger.warning(f"security_event={event} ip={ip} {extra}")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def validate_password_strength(password: str) -> Optional[str]:
    if not isinstance(password, str) or len(password) < 8 or len(password) > 72:
        return "Password must be 8-72 characters long"
    if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password)
            and re.search(r"\d", password) and re.search(r"[^A-Za-z0-9]", password)):
        return "Password must include upper and lower case letters, a number and a symbol"
    return None


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def create_token(data: dict, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": data["sub"],
        "role": data["role"],
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(hours=EXPIRE_HOURS),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": uuid.uuid4().hex,
        "tv": token_version,
        "typ": "access",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[Dict]:
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
            options={"require_exp": True, "require_iat": True, "require_sub": True, "require_jti": True},
        )
    except JWTError:
        return None
    if payload.get("typ") != "access" or payload.get("role") not in ROLES:
        return None
    return payload


async def revoke_token(payload: Dict):
    expires = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    await db.revoked_tokens.update_one(
        {"jti": payload["jti"]}, {"$set": {"jti": payload["jti"], "expires_at": expires}}, upsert=True
    )


async def bump_token_version(user_id: str):
    """Invalidates every session issued for the user."""
    await db.users.update_one({"id": user_id}, {"$inc": {"token_version": 1}})
    from routes.ws_routes import manager
    await manager.close_user(user_id)


async def authenticate_token(token: str) -> tuple[dict, Dict]:
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if await db.revoked_tokens.find_one({"jti": payload["jti"]}):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has been signed out")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    if payload.get("tv", 0) != user.get("token_version", 0) or payload["role"] != user.get("role"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid")
    if not user.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")
    return user, payload


def csrf_token_for(payload: Optional[Dict]) -> Optional[str]:
    """CSRF token bound to the session (HMAC of its jti). It survives contexts where the browser
    drops the readable CSRF cookie (embedded previews / alias domains), and can't be forged cross-site."""
    if not payload or not payload.get("jti"):
        return None
    return hmac.new(SECRET_KEY.encode(), f"csrf:{payload['jti']}".encode(), hashlib.sha256).hexdigest()


def session_csrf(token: str) -> Optional[str]:
    return csrf_token_for(decode_token(token)) if token else None


def issue_session(response: Response, user: dict) -> str:
    token = create_token({"sub": user["id"], "role": user["role"]}, user.get("token_version", 0))
    max_age = EXPIRE_HOURS * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=True, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, session_csrf(token), max_age=max_age, httponly=False, secure=True, samesite="lax", path="/")
    return token


def clear_session(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, samesite="lax")


def extract_token(request: Request, credentials: Optional[HTTPAuthorizationCredentials]) -> tuple[Optional[str], bool]:
    if credentials and credentials.credentials:
        return credentials.credentials, False
    return request.cookies.get(SESSION_COOKIE), True


async def get_current_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    token, from_cookie = extract_token(request, credentials)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if from_cookie and request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        expected = request.cookies.get(CSRF_COOKIE, "")
        bound = session_csrf(token) or ""
        ok = bool(sent) and ((expected and hmac.compare_digest(sent, expected)) or (bound and hmac.compare_digest(sent, bound)))
        if not ok:
            log_security_event("csrf_rejected", request, path=request.url.path,
                               header=bool(sent), cookie=bool(expected),
                               origin=request.headers.get("origin", "-"),
                               csrf_cookies=request.headers.get("cookie", "").count(f"{CSRF_COOKIE}="))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")
    user, payload = await authenticate_token(token)
    request.state.token_payload = payload
    return user


async def get_optional_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    token, _ = extract_token(request, credentials)
    if not token:
        return None
    try:
        user, _ = await authenticate_token(token)
        return user
    except HTTPException:
        return None


def require_role(*roles):
    async def checker(request: Request, current_user: dict = Depends(get_current_user)):
        if current_user.get("role") not in roles:
            log_security_event("authz_denied", request, user=current_user["id"], path=request.url.path)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return current_user
    return checker


def user_to_response(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in ("password_hash", "_id", "token_version") and not k.startswith("_token_")}
