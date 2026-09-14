import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError

from src.database import get_connection

# This repo is public on GitHub - a hardcoded fallback secret here would be
# visible to anyone and would let them forge valid officer tokens on any
# deployment that forgot to set EIS_SECRET_KEY. Instead, fall back to a
# random secret generated fresh per process start: local dev still works
# with zero setup, but every restart invalidates old tokens (a mild
# inconvenience) rather than shipping a guessable one (a real
# vulnerability). Set EIS_SECRET_KEY explicitly for any real deployment so
# tokens survive restarts.
SECRET_KEY = os.environ.get("EIS_SECRET_KEY")
if not SECRET_KEY:
    print(
        "WARNING: EIS_SECRET_KEY not set - using a random per-process secret. "
        "Officer tokens will stop working on restart. Set EIS_SECRET_KEY "
        "(32+ random bytes) before any real deployment."
    )
    SECRET_KEY = secrets.token_hex(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def authenticate_officer(username: str, password: str):
    conn = get_connection()
    row = conn.execute("SELECT * FROM officers WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row is None or not verify_password(password, row["hashed_password"]):
        return None
    return row


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": subject, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_officer(token: str = Depends(oauth2_scheme)) -> str:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str | None = payload.get("sub")
        if username is None:
            raise credentials_exception
        return username
    except PyJWTError:
        raise credentials_exception
