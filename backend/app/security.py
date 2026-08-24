from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
import jwt
from pwdlib import PasswordHash
from .config import settings

password_hash = PasswordHash.recommended()
fernet = Fernet(settings.field_encryption_key.encode())
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, password_hash_value: str) -> bool:
    return password_hash.verify(password, password_hash_value)


def create_access_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
    return jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str:
    return str(jwt.decode(token, settings.app_secret_key, algorithms=[ALGORITHM])["sub"])


def encrypt_secret(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()
