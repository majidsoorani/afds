"""Auth endpoints — login, token refresh, current user info."""

from fastapi import APIRouter, Depends, HTTPException
from typing import Annotated

from app.core.security import (
    LoginRequest, TokenResponse, TokenData,
    authenticate_user, create_access_token, get_current_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Authenticate and receive a JWT token."""
    user = authenticate_user(body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user["username"], "role": user["role"].value})
    return TokenResponse(access_token=token, role=user["role"].value)


@router.get("/me")
async def me(user: Annotated[TokenData, Depends(get_current_user)]):
    """Return the current authenticated user."""
    return {"username": user.sub, "role": user.role.value}
