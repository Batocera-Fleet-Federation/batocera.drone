"""The shared boundary every optional Drone integration plugs into.

Deliberately small and static: integrations are reviewed application code
registered in ``build_integration_registry`` -- there is no dynamic plugin
loading. Each integration owns its lifecycle, configuration, diagnostics and
documentation, keeps its files under ``<install root>/integrations/<id>/``,
and is strictly local to the machine running this Drone (never proxied to a
peer). The Admin -> Integrations page renders every registered integration
from ``IntegrationRegistry.cards()``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


HEALTH_VALUES = ("disabled", "healthy", "installing", "waiting", "degraded", "error")


@dataclass(frozen=True)
class IntegrationDescriptor:
    id: str
    name: str
    description: str
    icon: str
    configure_route: str
    capabilities: Tuple[str, ...] = field(default_factory=tuple)
    documentation: str = ""


class Integration(ABC):
    """Lifecycle contract. Long operations return a job descriptor instead of blocking."""

    descriptor: IntegrationDescriptor

    @abstractmethod
    def get_status(self) -> dict:
        """Full integration status for its own page."""

    @abstractmethod
    def card_status(self) -> dict:
        """Card summary: installed, enabled, health, health_message, version, metrics."""

    @abstractmethod
    def install(self, progress: Optional[Callable[[str], None]] = None) -> dict: ...

    @abstractmethod
    def enable(self, requested_by: str = "") -> dict: ...

    @abstractmethod
    def disable(self, requested_by: str = "") -> dict: ...

    @abstractmethod
    def repair(self, *, reinstall: bool = False, requested_by: str = "") -> dict: ...

    @abstractmethod
    def remove(self, *, include_configuration: bool = False, requested_by: str = "") -> dict: ...


def _error_card(descriptor: IntegrationDescriptor, error: Exception) -> dict:
    return {"installed": False, "enabled": False, "health": "error",
            "health_message": f"Status unavailable: {error}", "version": "", "metrics": []}


class IntegrationRegistry:
    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[IntegrationDescriptor, Callable[[], Integration]]] = {}

    def register(self, descriptor: IntegrationDescriptor, provider: Callable[[], Integration]) -> None:
        if not descriptor.id or descriptor.id in self._entries:
            raise ValueError(f"integration id must be unique: {descriptor.id!r}")
        self._entries[descriptor.id] = (descriptor, provider)

    def ids(self) -> List[str]:
        return list(self._entries)

    def get(self, integration_id: str) -> Integration:
        try:
            return self._entries[integration_id][1]()
        except KeyError as error:
            raise KeyError(f"unknown integration: {integration_id}") from error

    def cards(self) -> List[dict]:
        cards = []
        for descriptor, provider in self._entries.values():
            try:
                status = provider().card_status()
            except Exception as error:  # noqa: BLE001 - one broken integration must not hide the rest
                status = _error_card(descriptor, error)
            health = status.get("health") if status.get("health") in HEALTH_VALUES else "error"
            cards.append({
                "id": descriptor.id,
                "name": descriptor.name,
                "description": descriptor.description,
                "icon": descriptor.icon,
                "configure_route": descriptor.configure_route,
                "capabilities": list(descriptor.capabilities),
                "documentation": descriptor.documentation,
                "installed": bool(status.get("installed")),
                "enabled": bool(status.get("enabled")),
                "health": health,
                "health_message": str(status.get("health_message") or ""),
                "version": str(status.get("version") or ""),
                "metrics": list(status.get("metrics") or []),
            })
        return cards


def build_integration_registry(settings: Any, repository: Any) -> IntegrationRegistry:
    """Every integration this Drone ships, registered in one reviewed place."""
    try:
        from .streamdeck.manager import DESCRIPTOR as STREAMDECK, get_streamdeck_integration
    except ImportError:  # pragma: no cover - flat execution
        from integrations.streamdeck.manager import DESCRIPTOR as STREAMDECK, get_streamdeck_integration  # type: ignore

    registry = IntegrationRegistry()
    registry.register(STREAMDECK, lambda: get_streamdeck_integration(settings, repository))
    return registry
