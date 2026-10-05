"""Optional local hardware/software integrations (Admin -> Integrations)."""

from .registry import Integration, IntegrationDescriptor, IntegrationRegistry, build_integration_registry

__all__ = ["Integration", "IntegrationDescriptor", "IntegrationRegistry", "build_integration_registry"]
