import hashlib
import secrets
import string
from pathlib import Path
from fastapi.responses import FileResponse, RedirectResponse
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
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

DATABASE_URL = "sqlite:///./shortener.db"  # swap for Postgres later
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()

ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7
IP_SALT = "change-this-to-a-random-string"  # used when hashing IPs


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
app = FastAPI(title="URL Shortener")


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


@app.post("/api/links", status_code=201)
def create_link(payload: LinkCreate, db: Session = Depends(get_db)):
    if payload.alias:
        if not payload.alias.isalnum() or len(payload.alias) > 32:
            raise HTTPException(400, "Alias must be alphanumeric, max 32 chars")
        if db.query(Link).filter_by(code=payload.alias).first():
            raise HTTPException(409, "Alias already taken")
        code = payload.alias
    else:
        code = generate_code(db)

    link = Link(code=code, long_url=str(payload.url), expires_at=payload.expires_at)
    db.add(link)
    db.commit()
    return {"code": code, "short_url": f"http://localhost:8000/{code}"}


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


@app.get("/{code}")
def redirect(
    code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    link = db.query(Link).filter_by(code=code).first()
    if not link:
        raise HTTPException(404, "Link not found")
    if link.expires_at and link.expires_at < utcnow():
        raise HTTPException(410, "Link has expired")

    background_tasks.add_task(
        log_click,
        link.id,
        request.headers.get("referer"),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else "",
    )
    return RedirectResponse(link.long_url, status_code=302)

@app.get("/dashboard/{code}")
def dashboard(code: str):
    return FileResponse(Path(__file__).parent / "dashboard.html")