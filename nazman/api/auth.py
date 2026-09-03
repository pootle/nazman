from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..auth import authenticate_user, create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """Authenticate with the configured password and return a JWT token.

    When no password hash is configured (first-time setup), any password is
    accepted. Set AUTH_PASSWORD_HASH in /etc/nazman/nazman.conf to enforce one.
    """
    if not authenticate_user(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": "admin"})
    return {"token": token, "username": "admin"}
