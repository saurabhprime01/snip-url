import secrets
import string
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


DATABASE_URL = "sqlite:///./shortener.db"  #swap for Postgres later
engine = create_engine(DATABASE_URL, connect_args = {"check_same_thread": False})
SessionLocal = sessionmaker(bind= engine, autoflush=False)
Base = declarative_base()


ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Link(Base):
    __tablename__ = "links"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True, nullable=False)
    long_url = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable= True)


Base.metadata.create_all(engine)
app = FastAPI(title="URL Shortener")


def get_db():
    db = SessionLocal()
    try:
        yield db 
    finally:
        db.close()

class LinkCreate(BaseModel):
    url:HttpUrl
    alias: str | None = None
    expires_at: datetime | None = None

def generate_code(db: Session) -> str:
    for _ in range(5):
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if not db.query(Link).filter_by(code= code).first():
            return code
        raise HTTPException(500, "Could not generate a unique code")


@app.post("/api/links", status_code=201)
def create_link(payload: LinkCreate, db: Session = Depends(get_db)):
    if payload.alias:
        if not payload.alias.isalnum() or len(payload.alias) > 32:
            raise   HTTPException(400, "Alias must be alphanumeric, max 32 chars")
        if db.query(Link).filter_by(code=payload.alias).first():
            raise HTTPException(409, "Alias already taken")
        code = payload.alias
    else:
        code = generate_code(db)

    link = Link(code=code, long_url=str(payload.url), expires_at=payload.expires_at)
    db.add(link)
    db.commit()
    return {"code": code, "short_url": f"http://localhost:8000/{code}"}
@app.get("/{code}")
def redirect(code: str, db: Session = Depends(get_db)):
    link = db.query(Link).filter_by(code=code).first()
    if not link:
        raise HTTPException(404, "Link not found")
    if link.expires_at and link.expires_at < utcnow():
        raise HTTPException(410, "Link has expired")
    return RedirectResponse(link.long_url, status_code=302)

