"""Pydantic request/response models for the Drone's FastAPI surface.

An ``ExtensibleContractModel`` (extra="allow") base so declared fields are documented in
OpenAPI while any additional keys pass through unchanged.

IMPORTANT: imports pydantic, so this module is loaded only by the vendored FastAPI layer
(behind the DRONE_API_FASTAPI_BRIDGE flag) and by tests — never by the stdlib runtime path.
"""

from typing import Optional

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
