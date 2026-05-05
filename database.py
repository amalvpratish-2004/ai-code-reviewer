import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy import Column, String, Integer, DateTime
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

raw_url = os.getenv("DATABASE_URL")
# Strip all query params, swap driver
clean_url = raw_url.split("?")[0].replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(clean_url, connect_args={"ssl": "require"})
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class PullRequest(Base):
    __tablename__ = "pull_requests"

    id = Column(Integer, primary_key=True)
    repo = Column(String, nullable=False)
    pr_number = Column(Integer, nullable=False)
    pr_title = Column(String)
    opened_at = Column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)