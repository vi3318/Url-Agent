"""
Authentication Factory
======================
Auto-detects portal type from URL and returns the correct auth handler.

Adding a new portal:
    1. Create a handler class inheriting from ``BaseAuthHandler``
    2. Call ``AuthFactory.register(handler_class)``
    3. The factory will auto-detect the portal from URLs and use it

The factory is the ONLY entry point the crawler uses for authentication.
The crawler never imports SAP, Salesforce, or ServiceNow code directly.

Usage::

    from crawler.auth.auth_factory import AuthFactory

    handler = AuthFactory.detect(url)       # auto-detect from URL
    if handler:
        creds = handler.resolve_credentials()
        # ... login flow
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Type

from .base_auth import BaseAuthHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler Registry
# ---------------------------------------------------------------------------

# Global registry: maps portal name → handler class
_HANDLER_REGISTRY: Dict[str, Type[BaseAuthHandler]] = {}


class AuthFactory:
    """Factory for portal-specific auth handlers.

    Uses a registry pattern — handlers self-register and the factory
    selects the right one based on URL pattern matching.
    """

    @staticmethod
    def register(handler_class: Type[BaseAuthHandler]) -> None:
        """Register a handler class in the global registry.

        Args:
            handler_class: A concrete subclass of ``BaseAuthHandler``.
        """
        # Instantiate once to get the portal_name
        instance = handler_class()
        name = instance.portal_name.lower()
        _HANDLER_REGISTRY[name] = handler_class
        logger.debug(f"[AUTH-FACTORY] Registered handler: {name}")

    @staticmethod
    def detect(url: str) -> Optional[BaseAuthHandler]:
        """Auto-detect the portal type from a URL.

        Iterates through all registered handlers and returns the first
        one whose ``detect(url)`` returns True.

        Args:
            url: The target URL to crawl (e.g. ``https://me.sap.com/home``).

        Returns:
            An instantiated handler, or None if no handler matches.
        """
        url_lower = url.lower()
        for name, handler_class in _HANDLER_REGISTRY.items():
            handler = handler_class()
            if handler.detect(url_lower):
                logger.info(
                    f"[AUTH-FACTORY] Detected portal: {handler.portal_name} "
                    f"(from URL: {url[:60]})"
                )
                return handler

        logger.debug(f"[AUTH-FACTORY] No portal detected for: {url[:60]}")
        return None

    @staticmethod
    def get_handler(portal_name: str) -> Optional[BaseAuthHandler]:
        """Get a handler by explicit portal name.

        Args:
            portal_name: e.g. ``"sap"``, ``"salesforce"``, ``"servicenow"``

        Returns:
            An instantiated handler, or None if not registered.
        """
        handler_class = _HANDLER_REGISTRY.get(portal_name.lower())
        if handler_class:
            return handler_class()
        return None

    @staticmethod
    def list_handlers() -> List[str]:
        """Return names of all registered handlers."""
        return list(_HANDLER_REGISTRY.keys())

    @staticmethod
    def is_auth_required(url: str) -> bool:
        """Quick check: does any registered handler claim this URL?"""
        return AuthFactory.detect(url) is not None


# ---------------------------------------------------------------------------
# Auto-register built-in handlers on import
# ---------------------------------------------------------------------------

def _auto_register() -> None:
    """Import and register all built-in auth handlers.

    Called once at module load time.  Each handler's ``detect()`` determines
    whether it handles a given URL — no manual mapping needed.
    """
    try:
        from .sap_auth import SAPAuthHandler
        AuthFactory.register(SAPAuthHandler)
    except ImportError as e:
        logger.debug(f"[AUTH-FACTORY] SAP handler not available: {e}")

    # Future handlers — uncomment when implemented:
    # try:
    #     from .salesforce_auth import SalesforceAuthHandler
    #     AuthFactory.register(SalesforceAuthHandler)
    # except ImportError:
    #     pass
    #
    # try:
    #     from .servicenow_auth import ServiceNowAuthHandler
    #     AuthFactory.register(ServiceNowAuthHandler)
    # except ImportError:
    #     pass


_auto_register()
