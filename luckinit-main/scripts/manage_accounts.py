import argparse
import base64
import getpass
import hashlib
import hmac
import os
import shutil
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

PWD_ALGO = "pbkdf2_sha256"
PWD_ITERS = 180_000


def default_db_path() -> Path:
    scripts_dir = Path(__file__).resolve().parent
    project_root = scripts_dir.parent
    env_path = (os.getenv("LUCKINIT_DB_PATH") or "").strip()
    if env_path:
        db_path = Path(env_path).expanduser().resolve()
    else:
        db_path = (project_root / "cache.sqlite3").resolve()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    legacy_db = project_root / "cache.sqlite3"
    if db_path != legacy_db and not db_path.exists() and legacy_db.exists():
        try:
            src = sqlite3.connect(str(legacy_db))
            try:
                dst = sqlite3.connect(str(db_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error:
            try:
                shutil.copy2(legacy_db, db_path)
            except OSError:
                pass

    return db_path


def get_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path):
    now = int(time.time())
    with get_db(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        conn.commit()
    return now


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PWD_ITERS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    hash_b64 = base64.urlsafe_b64encode(dk).decode("ascii").rstrip("=")
    return f"{PWD_ALGO}${PWD_ITERS}${salt_b64}${hash_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != PWD_ALGO:
            return False
        iters = int(iters_s)
        salt = base64.urlsafe_b64decode(salt_b64 + "==")
        expected = base64.urlsafe_b64decode(hash_b64 + "==")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def cmd_list(db_path: Path):
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT id, username, is_active, created_at FROM users ORDER BY id ASC"
        ).fetchall()
    if not rows:
        print("No users.")
        return
    for r in rows:
        status = "active" if r["is_active"] else "disabled"
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))
        print(f'{r["id"]}\t{r["username"]}\t{status}\t{created}')


def cmd_add(db_path: Path, username: str, password: Optional[str]):
    init_db(db_path)
    username = username.strip()
    if not username:
        raise SystemExit("username required")

    if password is None:
        p1 = getpass.getpass("Password: ")
        p2 = getpass.getpass("Confirm: ")
        if p1 != p2:
            raise SystemExit("passwords do not match")
        password = p1

    pw_hash = hash_password(password)
    now = int(time.time())
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, is_active, created_at) VALUES (?, ?, 1, ?)",
            (username, pw_hash, now),
        )
        conn.commit()
    print(f"User created: {username}")


def cmd_delete(db_path: Path, username: str):
    init_db(db_path)
    username = username.strip()
    with get_db(db_path) as conn:
        user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise SystemExit("user not found")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        conn.commit()
    print(f"User deleted: {username}")


def cmd_passwd(db_path: Path, username: str, password: Optional[str]):
    init_db(db_path)
    username = username.strip()
    if password is None:
        p1 = getpass.getpass("New password: ")
        p2 = getpass.getpass("Confirm: ")
        if p1 != p2:
            raise SystemExit("passwords do not match")
        password = p1

    pw_hash = hash_password(password)
    with get_db(db_path) as conn:
        user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise SystemExit("user not found")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.commit()
    print(f"Password updated (sessions revoked): {username}")


def cmd_set_active(db_path: Path, username: str, active: bool):
    init_db(db_path)
    username = username.strip()
    with get_db(db_path) as conn:
        user = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            raise SystemExit("user not found")
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user["id"]))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.commit()
    print(f'User {"enabled" if active else "disabled"}: {username}')


def cmd_verify(db_path: Path, username: str):
    init_db(db_path)
    username = username.strip()
    password = getpass.getpass("Password: ")
    with get_db(db_path) as conn:
        user = conn.execute(
            "SELECT password_hash, is_active FROM users WHERE username = ?", (username,)
        ).fetchone()
    if not user or not user["is_active"]:
        raise SystemExit("invalid credentials")
    if not verify_password(password, user["password_hash"]):
        raise SystemExit("invalid credentials")
    print("OK")


def main():
    parser = argparse.ArgumentParser(description="Manage users for the Luckin site analyzer.")
    parser.add_argument(
        "--db",
        default=str(default_db_path()),
        help="SQLite DB path (default: LUCKINIT_DB_PATH or ./cache.sqlite3)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize user tables")

    p_list = sub.add_parser("list", help="List users")

    p_add = sub.add_parser("add", help="Add user")
    p_add.add_argument("username")
    p_add.add_argument("--password", help="Password (discouraged; use prompt instead)")

    p_del = sub.add_parser("delete", help="Delete user")
    p_del.add_argument("username")

    p_pw = sub.add_parser("passwd", help="Change password and revoke sessions")
    p_pw.add_argument("username")
    p_pw.add_argument("--password", help="Password (discouraged; use prompt instead)")

    p_dis = sub.add_parser("disable", help="Disable user and revoke sessions")
    p_dis.add_argument("username")

    p_en = sub.add_parser("enable", help="Enable user")
    p_en.add_argument("username")

    p_ver = sub.add_parser("verify", help="Verify a user's password")
    p_ver.add_argument("username")

    args = parser.parse_args()
    db_path = Path(args.db).expanduser().resolve()

    if args.cmd == "init":
        init_db(db_path)
        print(f"Initialized: {db_path}")
        return
    if args.cmd == "list":
        cmd_list(db_path)
        return
    if args.cmd == "add":
        cmd_add(db_path, args.username, args.password)
        return
    if args.cmd == "delete":
        cmd_delete(db_path, args.username)
        return
    if args.cmd == "passwd":
        cmd_passwd(db_path, args.username, args.password)
        return
    if args.cmd == "disable":
        cmd_set_active(db_path, args.username, active=False)
        return
    if args.cmd == "enable":
        cmd_set_active(db_path, args.username, active=True)
        return
    if args.cmd == "verify":
        cmd_verify(db_path, args.username)
        return


if __name__ == "__main__":
    main()
