"""Exceptions raised by the ports-client's Drone API client."""


class DroneApiError(Exception):
    """A Drone API call failed (non-2xx response, transport error, or bad JSON)."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


class AuthenticationError(DroneApiError):
    """Login failed, or a session expired mid-use (401 with no valid cookie)."""
