import pytest

from recon_domain.states import (
    CASE_TRANSITIONS,
    COMMAND_TERMINAL,
    COMMAND_TRANSITIONS,
    COMMAND_UNRESOLVED,
    CaseState,
    CommandState,
    IllegalTransition,
    ReservationState,
    check_case,
    check_command,
    reservation_for,
)

C = CommandState


def test_every_state_has_a_row():
    assert set(COMMAND_TRANSITIONS) == set(CommandState)
    assert set(CASE_TRANSITIONS) == set(CaseState)


def test_terminal_commands_have_no_exits():
    for s in COMMAND_TERMINAL:
        assert COMMAND_TRANSITIONS[s] == frozenset()


def test_unknown_never_goes_straight_back_to_prepared():
    with pytest.raises(IllegalTransition):
        check_command(C.UNKNOWN, C.PREPARED)
    with pytest.raises(IllegalTransition):
        check_command(C.SUBMITTING, C.PREPARED)


def test_success_cant_be_undone():
    with pytest.raises(IllegalTransition):
        check_command(C.CONFIRMED_SUCCESS, C.CONFIRMED_FAILURE)


def test_reservation_stays_active_until_terminal():
    for s in COMMAND_UNRESOLVED:
        assert reservation_for(s) is ReservationState.ACTIVE
    assert reservation_for(C.CONFIRMED_SUCCESS) is ReservationState.CONSUMED
    assert reservation_for(C.CONFIRMED_FAILURE) is ReservationState.RELEASED


def test_every_command_state_reachable_from_prepared():
    seen, todo = {C.PREPARED}, [C.PREPARED]
    while todo:
        for nxt in COMMAND_TRANSITIONS[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(CommandState)


def test_case_cant_close_from_executing_without_verify():
    with pytest.raises(IllegalTransition):
        check_case(CaseState.EXECUTING, CaseState.CLOSED)
    check_case(CaseState.VERIFYING, CaseState.CLOSED)
