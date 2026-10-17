import types
import typing

import pytest
from pydantic import BaseModel

from recon_agents.workflow import OUTPUTS


def models_in(root: type[BaseModel]) -> set[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    todo = [root]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        for f in m.model_fields.values():
            for arg in typing.get_args(f.annotation) or (f.annotation,):
                inner = typing.get_args(arg) or (arg,)
                todo += [a for a in inner if isinstance(a, type) and issubclass(a, BaseModel)]
    return seen


def nullable(annotation) -> bool:
    origin = typing.get_origin(annotation)
    return origin in (typing.Union, types.UnionType) and type(None) in typing.get_args(annotation)


@pytest.mark.parametrize("role", sorted(OUTPUTS))
def test_nullable_output_fields_have_defaults(role):
    # adk dumps agent output with exclude_none, a required nullable field fails on a legit null
    bad = [
        f"{m.__name__}.{name}"
        for m in models_in(OUTPUTS[role])
        for name, f in m.model_fields.items()
        if nullable(f.annotation) and f.is_required()
    ]
    assert not bad, bad
