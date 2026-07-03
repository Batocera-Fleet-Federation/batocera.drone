"""Pydantic request/response models for the Drone's FastAPI surface.

Mirrors the Overmind convention (batocera.overmind/src/overmind/models.py): an
``ExtensibleContractModel`` (extra="allow") base so declared fields are documented in OpenAPI
while any additional keys pass through unchanged.

IMPORTANT: imports pydantic, so this module is loaded only by the vendored FastAPI layer
(behind the DRONE_API_FASTAPI_BRIDGE flag) and by tests — never by the stdlib runtime path.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ExtensibleContractModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiInfoResponse(ExtensibleContractModel):
    """Metadata about the typed API surface (first FastAPI-served endpoint)."""
    name: str = "Batocera Drone API"
    app_version: Optional[str] = None
    api_prefix: str
    fastapi_bridge: bool = True
    docs_url: str
    openapi_url: str
    migrated_paths: list[str] = Field(default_factory=list)


class OvermindIntegrationStatus(ExtensibleContractModel):
    configured: Optional[bool] = None
    integration_enabled: Optional[bool] = None
    integration_state: Optional[str] = None
    swarm_connection_status: Optional[str] = None
    requested_at: Optional[Any] = None
    last_started_at: Optional[Any] = None
    last_error: Optional[Any] = None
    last_onboarding_attempt: Optional[dict] = None
    notes: Optional[str] = None


class OvermindStatusResponse(ExtensibleContractModel):
    """GET /admin/integrations/overmind/status — Overmind integration state for the admin UI."""
    overmind_url: Optional[str] = None
    overmind_email: Optional[str] = None
    drone_name: Optional[str] = None
    machine_id: Optional[str] = None
    password_configured: Optional[bool] = None
    password_masked: Optional[str] = None
    auth_token_configured: Optional[bool] = None
    auth_token_masked: Optional[str] = None
    token_configured: Optional[bool] = None
    token_masked: Optional[str] = None
    status: Optional[OvermindIntegrationStatus] = None
    swarm: list[Any] = Field(default_factory=list)
    peer_checks: list[Any] = Field(default_factory=list)
    certificate: Optional[dict] = None
    network_mode: Optional[str] = None
    overmind_active: Optional[bool] = None
