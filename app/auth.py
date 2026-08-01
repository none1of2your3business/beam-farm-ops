import bcrypt

DEFAULT_USERS = [
    {
        "username": "admin",
        "display_name": "Admin",
        "password": "farm2026",
        "role": "owner",
    },
]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
