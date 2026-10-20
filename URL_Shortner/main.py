import hashlib
import queue
import secrets
import string
import threading
import time
import os
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime  import datetime, timezone
from pathlib import Path
from fastapi.responses import FileResponse, RedirectResponse
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, FileResponse, RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from user_agents import parse as parse_ua

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./shortener.db")  # swap for Postgres later
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()

ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7
IP_SALT = os.getenv("IP_SALT", "change-this-to-a-random-string")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/") # used when hashing IPs
RESERVED = {"api", "dashboard", "docs", "openapi", "redoc", "favicon"}


CACHE_TTL = 300  # seconds
CACHE_MAX = 10_000
RATE_LIMIT = 10  # create requests allowed...
RATE_WINDOW = 60  # ...per this many seconds, per IP
BATCH_SIZE = 50
BATCH_WAIT = 1.0  # seconds


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Link(Base):
    __tablename__ = "links"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True, nullable=False)
    long_url = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)


class Click(Base):
    __tablename__ = "clicks"
    id = Column(Integer, primary_key=True)
    link_id = Column(Integer, ForeignKey("links.id"), index=True, nullable=False)
    ts = Column(DateTime, default=utcnow, index=True)
    referrer = Column(String, nullable=True)
    browser = Column(String, nullable=True)
    os = Column(String, nullable=True)
    device = Column(String, nullable=True)
    ip_hash = Column(String, nullable=True)


Base.metadata.create_all(engine)
# app = FastAPI(title="URL Shortener")

# ---------- Async click pipeline (queue + worker thread) ----------
click_queue: "queue.Queue[dict | None]" = queue.Queue()



def build_click(ev: dict) -> Click:
    ua = parse_ua(ev["user_agent"] or "")
    if ua.is_bot:
        device = "bot"
    elif ua.is_mobile:
        device = "mobile"
    elif ua.is_tablet:
        device = "tablet"
    else:
        device = "desktop"
    ip_hash = hashlib.sha256((IP_SALT + ev["ip"]).encode()).hexdigest()[:16]
    return Click(
        link_id=ev["link_id"],
        ts=ev["ts"],
        referrer=ev["referrer"] or None,
        browser=ua.browser.family,
        os=ua.os.family,
        device=device,
        ip_hash=ip_hash,
    )


def write_batch(batch: list[dict]):
    db = SessionLocal()
    try:
        db.add_all([build_click(ev) for ev in batch])
        db.commit()
    except Exception as exc:  # never let the worker die
        db.rollback()
        print("click batch failed:", exc)
    finally:
        db.close()


def click_worker():
    stop = False
    while not stop:
        first = click_queue.get()
        if first is None:
            break
        batch = [first]
        deadline = time.monotonic() + BATCH_WAIT
        while len(batch) < BATCH_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = click_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if nxt is None:
                stop = True
                break
            batch.append(nxt)
        write_batch(batch)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = threading.Thread(target=click_worker, daemon=True)
    worker.start()
    yield
    click_queue.put(None)  # flush remaining clicks, then stop
    worker.join(timeout=5)


app = FastAPI(title="URL Shortener", lifespan=lifespan)


# ---------- Cache (in-memory, TTL) ----------
link_cache: dict[str, tuple[float, dict]] = {}
cache_lock = threading.Lock()


def get_link_cached(code: str, db: Session) -> dict | None:
    now = time.monotonic()
    with cache_lock:
        hit = link_cache.get(code)
        if hit and hit[0] > now:
            return hit[1]

    link = db.query(Link).filter_by(code=code).first()
    if not link:
        return None
    data = {
        "id": link.id,
        "long_url": link.long_url,
        "expires_at": link.expires_at,
    }
    with cache_lock:
        if len(link_cache) >= CACHE_MAX:
            link_cache.clear()
        link_cache[code] = (now + CACHE_TTL, data)
    return data


# ---------- Rate limiting (sliding window per IP) ----------
rate_hits: dict[str, deque] = defaultdict(deque)
rate_lock = threading.Lock()


def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with rate_lock:
        hits = rate_hits[ip]
        while hits and hits[0] <= now - RATE_WINDOW:
            hits.popleft()
        if len(hits) >= RATE_LIMIT:
            retry = int(RATE_WINDOW - (now - hits[0])) + 1
            raise HTTPException(
                429,
                f"Too many requests. Try again in {retry}s",
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)


# ---------- Helpers ----------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class LinkCreate(BaseModel):
    url: HttpUrl
    alias: str | None = None
    expires_at: datetime | None = None  # naive UTC, e.g. 2026-12-31T00:00:00


def generate_code(db: Session) -> str:
    for _ in range(5):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if not db.query(Link).filter_by(code=code).first():
            return code
    raise HTTPException(500, "Could not generate a unique code")

def breakdownn(db: Session, link_id: int, column) -> dict:
    rows = (
        db.query(column, func.count(Click.id))
        .filter(Click.link_id == link_id)
        .group_by(column)
        .order_by(func.count(Click.id).desc())
        .all()
    )
    return {(key or "unknown"): count for key, count in rows}
def log_click(link_id: int, referrer: str | None, user_agent: str, ip: str):
    """Runs in the background, after the redirect has already been sent."""
    ua = parse_ua(user_agent or "")
    if ua.is_bot:
        device = "bot"
    elif ua.is_mobile:
        device = "mobile"
    elif ua.is_tablet:
        device = "tablet"
    else:
        device = "desktop"

    ip_hash = hashlib.sha256((IP_SALT + ip).encode()).hexdigest()[:16]

    db = SessionLocal()  # own session: the request's session is already closed
    try:
        db.add(
            Click(
                link_id=link_id,
                referrer=referrer or None,
                browser=ua.browser.family,
                os=ua.os.family,
                device=device,
                ip_hash=ip_hash,
            )
        )
        db.commit()
    finally:
        db.close()


def breakdown(db: Session, link_id: int, column) -> dict:
    rows = (
        db.query(column, func.count(Click.id))
        .filter(Click.link_id == link_id)
        .group_by(column)
        .order_by(func.count(Click.id).desc())
        .all()
    )
    return {(key or "unknown"): count for key, count in rows}

# ---------- Routes ----------
@app.post("/api/links", status_code=201, dependencies=[Depends(rate_limit)])
def create_link(payload: LinkCreate, db: Session = Depends(get_db)):
    if payload.alias:
        if not payload.alias.isalnum() or len(payload.alias) > 32:
            raise HTTPException(400, "Alias must be alphanumeric, max 32 chars")
        if payload.alias.lower() in RESERVED:
            raise HTTPException(400, "This alias is reserved")
        if db.query(Link).filter_by(code=payload.alias).first():
            raise HTTPException(409, "Alias already taken")
        code = payload.alias
    else:
        code = generate_code(db)

    link = Link(code=code, long_url=str(payload.url), expires_at=payload.expires_at)
    db.add(link)
    db.commit()
    return {"code": code, "short_url": f"{BASE_URL}/{code}"}

@app.get("/api/links/{code}/stats")
def link_stats(code: str, db: Session = Depends(get_db)):
    link = db.query(Link).filter_by(code=code).first()
    if not link:
        raise HTTPException(404, "Link not found")

    total = db.query(func.count(Click.id)).filter(Click.link_id == link.id).scalar()
    unique = (
        db.query(func.count(func.distinct(Click.ip_hash)))
        .filter(Click.link_id == link.id)
        .scalar()
    )
    return {
        "code": link.code,
        "long_url": link.long_url,
        "total_clicks": total,
        "unique_visitors": unique,
        "browsers": breakdown(db, link.id, Click.browser),
        "devices": breakdown(db, link.id, Click.device),
        "os": breakdown(db, link.id, Click.os),
        "referrers": breakdown(db, link.id, Click.referrer),
    }


@app.get("/api/links/{code}/timeseries")
def link_timeseries(code: str, db: Session = Depends(get_db)):
    link = db.query(Link).filter_by(code=code).first()
    if not link:
        raise HTTPException(404, "Link not found")

    day = func.date(Click.ts)
    rows = (
        db.query(day, func.count(Click.id))
        .filter(Click.link_id == link.id)
        .group_by(day)
        .order_by(day)
        .all()
    )
    return {"labels": [r[0] for r in rows], "values": [r[1] for r in rows]}


@app.get("/dashboard/{code}")
def dashboard(code: str):
    return FileResponse(Path(__file__).parent / "dashboard.html")

@app.get("/{code}")
def redirect(code: str, request: Request, db: Session = Depends(get_db)):
    link = get_link_cached(code, db)
    if not link:
        raise HTTPException(404, "Link not found")
    if link["expires_at"] and link["expires_at"] < utcnow():
        raise HTTPException(410, "Link has expired")

    click_queue.put(
        {
            "link_id": link["id"],
            "ts": utcnow(),
            "referrer": request.headers.get("referer"),
            "user_agent": request.headers.get("user-agent", ""),
            "ip": request.client.host if request.client else "",
        }
    )
    return RedirectResponse(link["long_url"], status_code=302)