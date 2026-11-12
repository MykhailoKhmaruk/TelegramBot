"""Робота з базою даних SQLite: користувачі та нагадування про ліки."""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "meds.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT
            );
            CREATE TABLE IF NOT EXISTS meds (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id  INTEGER NOT NULL,   -- хто створив (лікар або сам користувач)
                target_id INTEGER NOT NULL,   -- кому надсилати нагадування
                name      TEXT    NOT NULL,
                food      TEXT    NOT NULL,   -- after / before / any
                times     TEXT    NOT NULL,   -- "08:00,14:00,20:00"
                comment   TEXT,
                active    INTEGER NOT NULL DEFAULT 1,
                interval_days INTEGER NOT NULL DEFAULT 1,  -- 1 = щодня, 2 = через день, 7 = раз на тиждень
                start_date    TEXT                         -- 'YYYY-MM-DD', від якої дати рахувати інтервал
            );
            """
        )
        # міграція старих баз
        cols = {r["name"] for r in c.execute("PRAGMA table_info(meds)")}
        if "interval_days" not in cols:
            c.execute("ALTER TABLE meds ADD COLUMN interval_days INTEGER NOT NULL DEFAULT 1")
        if "start_date" not in cols:
            c.execute("ALTER TABLE meds ADD COLUMN start_date TEXT")


# ---------- користувачі ----------

def save_user(user_id: int, username: str | None, first_name: str | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO users(user_id, username, first_name) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user_id, username, first_name),
        )


def get_user(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def find_user(query: str) -> dict | None:
    """Пошук за числовим ID або @username серед тих, хто вже запускав бота."""
    q = query.strip()
    with _conn() as c:
        if q.lstrip("-").isdigit():
            row = c.execute("SELECT * FROM users WHERE user_id=?", (int(q),)).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM users WHERE lower(username)=lower(?)", (q.lstrip("@"),)
            ).fetchone()
    return dict(row) if row else None


# ---------- ліки ----------

def _med(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["times"] = d["times"].split(",") if d["times"] else []
    return d


def add_med(owner_id: int, target_id: int, name: str, food: str, times: list[str], comment: str | None,
            interval_days: int = 1, start_date: str | None = None) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO meds(owner_id, target_id, name, food, times, comment, interval_days, start_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_id, target_id, name, food, ",".join(times), comment, interval_days, start_date),
        )
        return cur.lastrowid


def update_med(med_id: int, name: str, food: str, times: list[str], comment: str | None,
               interval_days: int = 1, start_date: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE meds SET name=?, food=?, times=?, comment=?, interval_days=?, start_date=? WHERE id=?",
            (name, food, ",".join(times), comment, interval_days, start_date, med_id),
        )


def get_med(med_id: int) -> dict | None:
    with _conn() as c:
        return _med(c.execute("SELECT * FROM meds WHERE id=?", (med_id,)).fetchone())


def delete_med(med_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM meds WHERE id=?", (med_id,))


def meds_for_user(user_id: int) -> list[dict]:
    """Ліки, які користувач створив, або які призначені йому."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM meds WHERE owner_id=? OR target_id=? ORDER BY id", (user_id, user_id)
        ).fetchall()
    return [_med(r) for r in rows]


def set_active_for_user(user_id: int, active: bool) -> list[dict]:
    with _conn() as c:
        c.execute(
            "UPDATE meds SET active=? WHERE owner_id=? OR target_id=?",
            (int(active), user_id, user_id),
        )
    return meds_for_user(user_id)


def all_active() -> list[dict]:
    with _conn() as c:
        return [_med(r) for r in c.execute("SELECT * FROM meds WHERE active=1").fetchall()]
