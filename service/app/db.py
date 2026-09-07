"""Database models and session helpers (SQLAlchemy 2.x).

Tables
------
orders          one row per TartanConnect purchase notification (= one checkout)
order_items     the size/quantity lines inside that purchase
pickups         one row per confirmed handoff (who, when, how many)
inbound_emails  every webhook we received, with how we classified it
"""
from __future__ import annotations

import datetime as dt
from typing import Generator, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pickup_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # TartanConnect's own identifier for the purchase when we can find one in
    # the notification (receipt/transaction number). Used for de-duplication.
    tc_reference: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    # Fallback dedup key: sha256 of (buyer_email, items, purchase minute).
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    buyer_name: Mapped[str] = mapped_column(String(200))
    buyer_email: Mapped[str] = mapped_column(String(320), index=True)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    purchased_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # pending | picked_up | needs_review | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # deterministic | llm | reconcile | manual
    parse_source: Mapped[str] = mapped_column(String(32), default="deterministic")
    source_message_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_thread_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # AgentMail ids of the code email we sent (so replies can be matched).
    code_email_message_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    code_email_thread_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    code_email_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    code_email_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    items: Mapped[List["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    pickups: Mapped[List["Pickup"]] = relationship(back_populates="order", cascade="all, delete-orphan")

    @property
    def quantity_total(self) -> int:
        return sum(i.quantity for i in self.items)

    @property
    def quantity_picked(self) -> int:
        return sum(p.quantity for p in self.pickups)

    def summary(self) -> str:
        return ", ".join(f"{i.quantity} x {i.label}" for i in self.items)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (UniqueConstraint("order_id", "product_name", name="uq_order_product"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_name: Mapped[str] = mapped_column(String(300))
    size: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_cents: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped[Order] = relationship(back_populates="items")

    @property
    def label(self) -> str:
        return f"{self.product_name}" if not self.size else f"{self.product_name} (size {self.size})"


class Pickup(Base):
    __tablename__ = "pickups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    picked_up_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    volunteer: Mapped[str] = mapped_column(String(200))
    presented_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # buyer or delegate name
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    order: Mapped[Order] = relationship(back_populates="pickups")


class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    message_id: Mapped[str] = mapped_column(String(512), index=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    from_addr: Mapped[str] = mapped_column(String(320))
    subject: Mapped[Optional[str]] = mapped_column(String(998), nullable=True)
    # purchase | buyer_reply | ignored | needs_review | error
    classification: Mapped[str] = mapped_column(String(32), default="ignored", index=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


engine = create_engine(
    settings.database_url_normalized,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def find_order_by_code(session: Session, code: str) -> Optional[Order]:
    from .codes import normalize_code

    normalized = normalize_code(code)
    if not normalized:
        return None
    return session.scalar(select(Order).where(Order.pickup_code == normalized))
