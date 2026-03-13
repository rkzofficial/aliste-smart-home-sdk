from __future__ import annotations


class AlisteError(Exception):
    """Base exception for all SDK errors."""


class AuthenticationError(AlisteError):
    """Raised when authentication or credential refresh fails."""


class ApiError(AlisteError):
    """Raised when an Aliste API request fails."""
