from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Ts = DateTime(timezone=True)
EMBED_DIM = 768  # nomic-embed-text, another model size needs a migration


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, datetime: Ts}


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str]
    provider_account: Mapped[str]
    created_at: Mapped[datetime]


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    email: Mapped[str]
    name: Mapped[str]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    customer_id: Mapped[str]
    total_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    placed_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
    )


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    order_id: Mapped[str]
    sku: Mapped[str]
    description: Mapped[str]
    kind: Mapped[str] = mapped_column(String, server_default="item")  # item | shipping
    quantity: Mapped[int] = mapped_column(Integer)
    unit_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
        CheckConstraint("quantity > 0 AND unit_minor >= 0", name="positive"),
    )


class Return(Base):
    __tablename__ = "returns"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    order_id: Mapped[str]
    line_id: Mapped[str]
    quantity: Mapped[int] = mapped_column(Integer)
    requested_at: Mapped[datetime]
    # null until the warehouse has it, an unreceived return isn't refundable yet
    received_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
        ForeignKeyConstraint(["tenant_id", "line_id"], ["order_lines.tenant_id", "order_lines.id"]),
        CheckConstraint("quantity > 0", name="positive"),
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    customer_id: Mapped[str]
    order_id: Mapped[str]
    plan_name: Mapped[str]
    period_start: Mapped[datetime]
    period_end: Mapped[datetime]
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    cancelled_at: Mapped[datetime | None]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "order_id"),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        CheckConstraint("period_end > period_start AND price_minor > 0", name="valid_period"),
    )


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    order_id: Mapped[str]
    customer_id: Mapped[str]
    provider_payment_id: Mapped[str]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    status: Mapped[str]
    captured_at: Mapped[datetime]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    refunded_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "provider_payment_id"),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        CheckConstraint("refunded_minor >= 0 AND reserved_minor >= 0", name="non_negative"),
        CheckConstraint("refunded_minor + reserved_minor <= amount_minor", name="not_overdrawn"),
    )


class RefundClaim(Base):
    __tablename__ = "refund_claims"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    entitlement_ref: Mapped[str]
    payment_id: Mapped[str]
    currency: Mapped[str]
    permitted_minor: Mapped[int] = mapped_column(BigInteger)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    consumed_minor: Mapped[int] = mapped_column(BigInteger, server_default="0")
    created_at: Mapped[datetime]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "entitlement_ref"),
        ForeignKeyConstraint(["tenant_id", "payment_id"], ["payments.tenant_id", "payments.id"]),
        CheckConstraint("reserved_minor >= 0 AND consumed_minor >= 0", name="non_negative"),
        CheckConstraint("reserved_minor + consumed_minor <= permitted_minor", name="not_overdrawn"),
    )


class PolicyDocument(Base):
    __tablename__ = "policy_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    authority_class: Mapped[str]
    version: Mapped[int]
    title: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[datetime]
    effective_to: Mapped[datetime | None]
    checksum: Mapped[str]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "authority_class", "version"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )


class PolicyRule(Base):
    __tablename__ = "policy_rules"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    policy_document_id: Mapped[str]
    refund_window_days: Mapped[int | None]
    duplicate_refund_allowed: Mapped[bool | None]
    rounding_mode: Mapped[str | None]
    returns_allowed: Mapped[bool | None]
    shipping_refundable: Mapped[bool | None]
    proration_allowed: Mapped[bool | None]
    auto_approve_limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (
        UniqueConstraint("policy_document_id"),
        ForeignKeyConstraint(
            ["tenant_id", "policy_document_id"], ["policy_documents.tenant_id", "policy_documents.id"]
        ),
    )


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    policy_document_id: Mapped[str]
    section_id: Mapped[str]
    heading: Mapped[str]
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', heading || ' ' || text)", persisted=True)
    )
    # filled by recon_retrieval.index, never loaded through the orm
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), deferred=True)
    embedding_model: Mapped[str | None]
    __table_args__ = (
        UniqueConstraint("policy_document_id", "section_id"),
        ForeignKeyConstraint(
            ["tenant_id", "policy_document_id"], ["policy_documents.tenant_id", "policy_documents.id"]
        ),
        Index("ix_policy_chunks_tsv", "tsv", postgresql_using="gin"),
    )


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    customer_id: Mapped[str]
    order_id: Mapped[str | None]
    subject: Mapped[str]
    state: Mapped[str]
    resolution: Mapped[str | None]
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
    )


class CaseMessage(Base):
    __tablename__ = "case_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str]
    author: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str]
    stage: Mapped[str]
    investigation: Mapped[str]
    workflow_version: Mapped[str]
    models: Mapped[dict[str, Any]]
    budget: Mapped[dict[str, Any]]
    adk_session_id: Mapped[str | None]
    adk_invocation_id: Mapped[str | None]
    evidence: Mapped[dict[str, Any] | None]
    failure: Mapped[str | None] = mapped_column(Text)
    model_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, server_default="0")
    est_cost_micros: Mapped[int] = mapped_column(BigInteger, server_default="0")
    plan_id: Mapped[str | None]
    command_id: Mapped[str | None]
    owns_command: Mapped[bool] = mapped_column(Boolean, server_default="false")
    reconcile_rounds: Mapped[int] = mapped_column(Integer, server_default="0")
    # lease + fence, times are db time so every worker reads one clock
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    seq: Mapped[int] = mapped_column(BigInteger, Identity())
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str]
    run_id: Mapped[str]
    stage: Mapped[str]
    event_id: Mapped[str] = mapped_column(String, unique=True)
    available_at: Mapped[datetime] = mapped_column(Ts, server_default=text("now()"))
    dispatched_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # parked after too many failed deliveries, a human looks at these
    dead_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(Ts, server_default=text("now()"))
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"]),
        Index("ix_outbox_due", "available_at", postgresql_where=text("dispatched_at IS NULL")),
    )


class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str]
    provider: Mapped[str]
    event_id: Mapped[str]
    type: Mapped[str]
    payload_hash: Mapped[str]
    payload: Mapped[dict[str, Any]]
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime | None]
    outcome: Mapped[str | None]
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "event_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )


class FaultPoint(Base):
    # dev-only schedule, read only when RECON_FAULTS=1
    __tablename__ = "fault_points"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    point: Mapped[str]
    scope_id: Mapped[str | None]
    remaining: Mapped[int] = mapped_column(Integer, server_default="1")


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str]
    run_id: Mapped[str]
    agent: Mapped[str]
    model: Mapped[str]
    invocation_id: Mapped[str | None]
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    cached_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    thoughts_tokens: Mapped[int] = mapped_column(Integer)
    tool_prompt_tokens: Mapped[int] = mapped_column(Integer)
    est_cost_micros: Mapped[int] = mapped_column(BigInteger)
    pricing_snapshot: Mapped[str]
    latency_ms: Mapped[int | None]
    raw: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    __table_args__ = (ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"]),)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str]
    case_version: Mapped[int]
    run_id: Mapped[str | None]
    source: Mapped[str]
    payload: Mapped[dict[str, Any]]
    material_hash: Mapped[str]
    entitlement_ref: Mapped[str | None]
    decision: Mapped[str]
    verdict: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]
    seq: Mapped[int] = mapped_column(BigInteger, Identity())  # insert order, "latest plan" can't tie
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"]),
    )


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str]
    plan_id: Mapped[str]
    # what was approved, all of it, so a changed plan or amount can't ride on an old yes
    plan_hash: Mapped[str]
    payment_id: Mapped[str | None]
    entitlement_ref: Mapped[str | None]
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None]
    approver: Mapped[str]
    role: Mapped[str]
    decision: Mapped[str]  # APPROVED | REJECTED
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]
    # insert order, timestamps can tie and the newest decision has to win
    seq: Mapped[int] = mapped_column(BigInteger, Identity())
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        # several per plan is fine (an expired yes, then a fresh one), prepare uses the newest
        Index("ix_approvals_plan", "tenant_id", "plan_id", "seq"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(["tenant_id", "plan_id"], ["plans.tenant_id", "plans.id"]),
    )


class Command(Base):
    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str]
    plan_id: Mapped[str]
    claim_id: Mapped[str]
    entitlement_ref: Mapped[str]
    op_seq: Mapped[int]
    payment_id: Mapped[str]
    provider_payment_id: Mapped[str]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    payload_hash: Mapped[str]
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    state: Mapped[str]
    provider_refund_id: Mapped[str | None] = mapped_column(String, unique=True)
    first_submitted_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "entitlement_ref", "op_seq"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(["tenant_id", "plan_id"], ["plans.tenant_id", "plans.id"]),
        ForeignKeyConstraint(["tenant_id", "claim_id"], ["refund_claims.tenant_id", "refund_claims.id"]),
        ForeignKeyConstraint(["tenant_id", "payment_id"], ["payments.tenant_id", "payments.id"]),
        CheckConstraint("amount_minor > 0", name="positive"),
        # one live command per entitlement, failed ones can be retried with a new op_seq
        Index(
            "uq_commands_live_per_entitlement",
            "tenant_id",
            "entitlement_ref",
            unique=True,
            postgresql_where=text("state <> 'CONFIRMED_FAILURE'"),
        ),
    )


class CommandAttempt(Base):
    __tablename__ = "command_attempts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    command_id: Mapped[str]
    attempt_no: Mapped[int]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    outcome: Mapped[str | None]
    http_status: Mapped[int | None]
    error_type: Mapped[str | None]
    error_code: Mapped[str | None]
    provider_refund_id: Mapped[str | None]
    replayed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    __table_args__ = (
        UniqueConstraint("command_id", "attempt_no"),
        ForeignKeyConstraint(["tenant_id", "command_id"], ["commands.tenant_id", "commands.id"]),
    )


class PaymentReservation(Base):
    __tablename__ = "payment_reservations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str]
    command_id: Mapped[str] = mapped_column(String, unique=True)
    payment_id: Mapped[str]
    claim_id: Mapped[str]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    state: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "command_id"], ["commands.tenant_id", "commands.id"]),
        CheckConstraint("amount_minor > 0", name="positive"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str]
    case_id: Mapped[str | None]
    command_id: Mapped[str | None]
    actor: Mapped[str]
    kind: Mapped[str]
    data: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),)


# one row per client Idempotency-Key, a retried write gets the first answer back
class ApiRequest(Base):
    __tablename__ = "api_requests"
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    route: Mapped[str]
    request_hash: Mapped[str]
    status_code: Mapped[int | None]
    response: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str]
    failed_logins: Mapped[int] = mapped_column(Integer, server_default="0")
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime]


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str]
    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["users.id"]),
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        CheckConstraint("role IN ('operator', 'supervisor')", name="role"),
    )
