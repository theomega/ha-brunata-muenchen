from __future__ import annotations


class BrunataError(Exception):
    """Base error for this package."""


class UnsupportedPortalError(BrunataError):
    """The portal responded with content we can't handle (e.g. JS-only / UA gated)."""


class LoginError(BrunataError):
    """Login failed (bad credentials, missing token/cookie pair, blocked client, etc.)."""


class DiscoveryError(BrunataError):
    """Could not discover portal pages/endpoints after login."""

