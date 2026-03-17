from collections.abc import AsyncIterator
from typing import Protocol

from aidomaincontext.schemas.documents import DocumentBase


class ConnectorProtocol(Protocol):
    connector_type: str

    async def validate_credentials(self, config: dict) -> bool: ...

    async def fetch_documents(
        self, config: dict, cursor: dict | None
    ) -> AsyncIterator[tuple[DocumentBase, dict]]: ...

    async def handle_webhook(self, payload: dict) -> list[DocumentBase]: ...


# Registry of available connectors
_registry: dict[str, type] = {}


def register_connector(cls: type) -> type:
    _registry[cls.connector_type] = cls
    return cls


def get_connector(connector_type: str) -> ConnectorProtocol:
    cls = _registry[connector_type]
    return cls()
