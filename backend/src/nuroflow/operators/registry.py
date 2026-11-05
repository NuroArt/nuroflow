"""Operator registry (Open/Closed: new task types register here; the engine never changes).

Third-party packages can ship operators without touching NuroFlow by exposing an
entry point::

    [project.entry-points."nuroflow.operators"]
    hubspot = "nuroflow_hubspot:make_operator"   # zero-arg callable -> Operator
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any

import httpx

from nuroflow.integrations.llm.base import LLMProvider
from nuroflow.integrations.telegram.base import TelegramClient
from nuroflow.operators.base import Operator
from nuroflow.operators.builtin.ai import AICompleteOperator, AIStructuredOperator
from nuroflow.operators.builtin.core import DelayOperator, NoopOperator, TransformOperator
from nuroflow.operators.builtin.data import DataStore, DataStoreOperator
from nuroflow.operators.builtin.http import HttpRequestOperator
from nuroflow.operators.builtin.telegram import TelegramSendOperator

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nuroflow.operators"


class UnknownOperatorError(KeyError):
    pass


class OperatorRegistry:
    def __init__(self, operators: Iterable[Operator] = ()) -> None:
        self._ops: dict[str, Operator] = {}
        for op in operators:
            self.register(op)

    def register(self, operator: Operator) -> None:
        if operator.name in self._ops:
            raise ValueError(f"operator '{operator.name}' is already registered")
        self._ops[operator.name] = operator

    def get(self, name: str) -> Operator:
        try:
            return self._ops[name]
        except KeyError:
            raise UnknownOperatorError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._ops

    @property
    def names(self) -> list[str]:
        return sorted(self._ops)

    def describe(self) -> list[dict[str, Any]]:
        return [self._ops[n].describe() for n in self.names]

    def load_entry_points(self) -> int:
        loaded = 0
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                self.register(ep.load()())
                loaded += 1
            except Exception:
                logger.exception("failed to load operator plugin", extra={"entry_point": ep.name})
        return loaded


def build_registry(
    *,
    llm_providers: Mapping[str, LLMProvider],
    telegram: TelegramClient,
    data_store: DataStore,
    allow_private_networks: bool = False,
    http_transport: httpx.BaseTransport | None = None,
    load_plugins: bool = True,
) -> OperatorRegistry:
    registry = OperatorRegistry(
        [
            NoopOperator(),
            DelayOperator(),
            TransformOperator(),
            HttpRequestOperator(
                allow_private_networks=allow_private_networks, transport=http_transport
            ),
            DataStoreOperator(data_store),
            AICompleteOperator(llm_providers),
            AIStructuredOperator(llm_providers),
            TelegramSendOperator(telegram),
        ]
    )
    if load_plugins:
        registry.load_entry_points()
    return registry
