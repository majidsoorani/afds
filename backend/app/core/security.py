"""
AFDS Security — JWT authentication, RBAC, and password hashing.
"""

import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated

from fastapi import Depends, HTTPException, WebSocketException, status
from fastapi.security.utils import get_authorization_scheme_param
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from app.core.config import get_settings

settings = get_settings()

# ── Password hashing ────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ──────────────────────────────────────────────────────────────
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.backend_secret_key, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.backend_secret_key, algorithms=[ALGORITHM])


# ── Roles ────────────────────────────────────────────────────────────
class Role(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"
    SERVICE = "service"

class TokenData(BaseModel):
    sub: str
    role: Role = Role.VIEWER
    exp: datetime | None = None


# ── In-memory user store ─────────────────────────────────────────────
# Read passwords from env in prod (AFDS_USER_<NAME>=<password>); fall back
# to documented defaults for local/dev only.
def _pw(env_key: str, default: str) -> str:
    return hash_password(os.getenv(env_key, default))

_users: dict[str, dict] = {
    # ── Privileged ──
    "admin": {
        "username": "admin",
        "hashed_password": _pw("AFDS_USER_ADMIN", "afds-admin-2026"),
        "role": Role.ADMIN,
        "full_name": "AFDS Admin",
    },
    "majid": {
        "username": "majid",
        "hashed_password": _pw("AFDS_USER_MAJID", "afds-majid-2026"),
        "role": Role.ADMIN,
        "full_name": "Majid Soorani",
    },
    "devops": {
        "username": "devops",
        "hashed_password": _pw("AFDS_USER_DEVOPS", "afds-devops-2026"),
        "role": Role.ADMIN,
        "full_name": "Platform / DevOps",
    },

    # ── Analyst (read + alert triage + rule edits) ──
    "analyst": {
        "username": "analyst",
        "hashed_password": _pw("AFDS_USER_ANALYST", "afds-analyst-2026"),
        "role": Role.ANALYST,
        "full_name": "Compliance Analyst",
    },
    "farzad": {
        "username": "farzad",
        "hashed_password": _pw("AFDS_USER_FARZAD", "afds-farzad-2026"),
        "role": Role.ANALYST,
        "full_name": "Farzad Sedaghatbin",
    },
    "fraud-lead": {
        "username": "fraud-lead",
        "hashed_password": _pw("AFDS_USER_FRAUD_LEAD", "afds-fraudlead-2026"),
        "role": Role.ANALYST,
        "full_name": "Fraud Operations Lead",
    },
    "auditor": {
        "username": "auditor",
        "hashed_password": _pw("AFDS_USER_AUDITOR", "afds-auditor-2026"),
        "role": Role.ANALYST,
        "full_name": "Internal Auditor",
    },

    # ── Viewer (read-only dashboards) ──
    "viewer": {
        "username": "viewer",
        "hashed_password": _pw("AFDS_USER_VIEWER", "afds-viewer-2026"),
        "role": Role.VIEWER,
        "full_name": "Dashboard Viewer",
    },
    "demo": {
        "username": "demo",
        "hashed_password": _pw("AFDS_USER_DEMO", "afds-demo-2026"),
        "role": Role.VIEWER,
        "full_name": "Demo Account (read-only)",
    },
    "exec": {
        "username": "exec",
        "hashed_password": _pw("AFDS_USER_EXEC", "afds-exec-2026"),
        "role": Role.VIEWER,
        "full_name": "Executive Read-Only",
    },
}

# ── API keys (service-to-service) ────────────────────────────────────
# Override in prod via AFDS_API_KEY_<SERVICE>=<secret>.
def _key(env_key: str, default: str) -> str:
    return os.getenv(env_key, default)

_api_keys: dict[str, dict] = {
    _key("AFDS_API_KEY_MCP",     "afds-mcp-server-key"):  {"role": Role.SERVICE, "service": "mcp-server"},
    _key("AFDS_API_KEY_FLINK",   "afds-flink-key"):       {"role": Role.SERVICE, "service": "flink"},
    _key("AFDS_API_KEY_CRON",    "afds-cron-key"):        {"role": Role.SERVICE, "service": "cronjob"},
    _key("AFDS_API_KEY_GRAFANA", "afds-grafana-key"):     {"role": Role.SERVICE, "service": "grafana"},
}

# ── Auth dependencies ────────────────────────────────────────────────
async def get_current_user(conn: HTTPConnection) -> TokenData:
    """Extract user from JWT bearer token or API key.

    This dependency is attached at router level, including routers that contain
    WebSocket routes. FastAPI's HTTPBearer helper only accepts Request objects,
    so parsing the shared HTTPConnection keeps auth compatible with both HTTP
    and WebSocket scopes.
    """
    api_key = conn.headers.get("x-api-key") or conn.query_params.get("api_key")

    if api_key and api_key in _api_keys:
        info = _api_keys[api_key]
        return TokenData(sub=f"service:{info['service']}", role=info["role"])

    authorization = conn.headers.get("authorization")
    scheme, bearer_token = get_authorization_scheme_param(authorization)
    token = bearer_token if scheme.lower() == "bearer" else conn.query_params.get("token")

    if token:
        try:
            payload = decode_token(token)
            username = payload.get("sub")
            role = payload.get("role", "viewer")
            if not username:
                raise HTTPException(status_code=401, detail="Invalid token")
            return TokenData(sub=username, role=Role(role))
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    if conn.scope.get("type") == "websocket":
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing authentication",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_websocket(token: str | None, api_key: str | None) -> TokenData | None:
    """Authenticate a WebSocket connection from query-string token or api_key.

    Returns TokenData on success or None on failure. Caller is responsible
    for closing the socket with code 1008 on None.
    """
    if api_key and api_key in _api_keys:
        info = _api_keys[api_key]
        return TokenData(sub=f"service:{info['service']}", role=info["role"])
    if token:
        try:
            payload = decode_token(token)
            username = payload.get("sub")
            role = payload.get("role", "viewer")
            if username:
                return TokenData(sub=username, role=Role(role))
        except JWTError:
            return None
    return None


def require_role(*roles: Role):
    """Dependency that checks the user has one of the specified roles."""
    async def _check(user: Annotated[TokenData, Depends(get_current_user)]):
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"Requires role: {', '.join(r.value for r in roles)}")
        return user
    return _check


# ── Auth endpoints (login) ───────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60

def authenticate_user(username: str, password: str) -> dict | None:
    user = _users.get(username)
    if user and verify_password(password, user["hashed_password"]):
        return user
    return None
