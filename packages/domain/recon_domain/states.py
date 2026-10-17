from __future__ import annotations

from enum import StrEnum


class IllegalTransition(Exception):
    pass


class CommandState(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    PENDING_PROVIDER = "PENDING_PROVIDER"
    UNKNOWN = "UNKNOWN"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILURE = "CONFIRMED_FAILURE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ReservationState(StrEnum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"


class CaseState(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    VALIDATING = "VALIDATING"
    AWAITING_INFO = "AWAITING_INFO"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    RECONCILING = "RECONCILING"
    VERIFYING = "VERIFYING"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


C = CommandState
COMMAND_TRANSITIONS: dict[CommandState, frozenset[CommandState]] = {
    C.PREPARED: frozenset({C.SUBMITTING}),
    # 409/429/5xx/timeout all land in UNKNOWN, reconcile decides from there
    C.SUBMITTING: frozenset({C.CONFIRMED_SUCCESS, C.PENDING_PROVIDER, C.CONFIRMED_FAILURE, C.UNKNOWN, C.MANUAL_REVIEW}),
    C.UNKNOWN: frozenset({C.SUBMITTING, C.CONFIRMED_SUCCESS, C.PENDING_PROVIDER, C.CONFIRMED_FAILURE, C.MANUAL_REVIEW}),
    C.PENDING_PROVIDER: frozenset({C.CONFIRMED_SUCCESS, C.CONFIRMED_FAILURE, C.MANUAL_REVIEW}),
    # only an operator gets a command out of manual review
    C.MANUAL_REVIEW: frozenset({C.CONFIRMED_SUCCESS, C.CONFIRMED_FAILURE}),
    C.CONFIRMED_SUCCESS: frozenset(),
    C.CONFIRMED_FAILURE: frozenset(),
}

COMMAND_TERMINAL = frozenset({C.CONFIRMED_SUCCESS, C.CONFIRMED_FAILURE})
# outcome not settled, reservation must stay active
COMMAND_UNRESOLVED = frozenset({C.PREPARED, C.SUBMITTING, C.UNKNOWN, C.PENDING_PROVIDER, C.MANUAL_REVIEW})

R = ReservationState
RESERVATION_TRANSITIONS: dict[ReservationState, frozenset[ReservationState]] = {
    R.ACTIVE: frozenset({R.CONSUMED, R.RELEASED}),
    R.CONSUMED: frozenset(),
    R.RELEASED: frozenset(),
}

S = CaseState
CASE_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    S.OPEN: frozenset({S.INVESTIGATING}),
    S.INVESTIGATING: frozenset({S.VALIDATING, S.ESCALATED}),
    S.VALIDATING: frozenset(
        {S.EXECUTING, S.AWAITING_APPROVAL, S.AWAITING_INFO, S.CLOSED, S.ESCALATED, S.INVESTIGATING}
    ),
    S.AWAITING_APPROVAL: frozenset({S.VALIDATING, S.INVESTIGATING, S.CLOSED, S.ESCALATED}),
    S.AWAITING_INFO: frozenset({S.INVESTIGATING, S.ESCALATED}),
    S.EXECUTING: frozenset({S.RECONCILING, S.VERIFYING, S.ESCALATED}),
    S.RECONCILING: frozenset({S.VERIFYING, S.ESCALATED}),
    S.VERIFYING: frozenset({S.CLOSED, S.ESCALATED}),
    S.ESCALATED: frozenset({S.INVESTIGATING, S.CLOSED}),
    S.CLOSED: frozenset(),
}


def check_command(src: CommandState, dst: CommandState) -> None:
    if dst not in COMMAND_TRANSITIONS[src]:
        raise IllegalTransition(f"command {src} -> {dst}")


def check_reservation(src: ReservationState, dst: ReservationState) -> None:
    if dst not in RESERVATION_TRANSITIONS[src]:
        raise IllegalTransition(f"reservation {src} -> {dst}")


def check_case(src: CaseState, dst: CaseState) -> None:
    if dst not in CASE_TRANSITIONS[src]:
        raise IllegalTransition(f"case {src} -> {dst}")


def reservation_for(state: CommandState) -> ReservationState:
    if state is C.CONFIRMED_SUCCESS:
        return R.CONSUMED
    if state is C.CONFIRMED_FAILURE:
        return R.RELEASED
    return R.ACTIVE
