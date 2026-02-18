"""
Authentication Module
=====================
Modular authentication framework for enterprise portal crawling.

Architecture:
    - ``BaseAuthHandler``  — abstract base class for all portal handlers
    - ``AuthFactory``      — auto-detects portal type from URL
    - ``SessionStore``     — manages session persistence and reuse
    - ``Credentials``      — credential container (resolved from env/prompt)

Built-in handlers:
    - ``SAPAuthHandler``   — SAP portals (SAML SSO, Fiori, SuccessFactors)

Extending:
    To add a new portal (e.g. Salesforce), create a handler that inherits
    from ``BaseAuthHandler``, implement the abstract methods, and register
    it in ``auth_factory.py``. No changes to the crawler core are needed.

Legacy compatibility:
    - ``LoginManager``     — generic form-based login (still available)
    - ``SAPLoginHandler``  — legacy SAP handler (use SAPAuthHandler instead)
    - ``SessionManager``   — legacy session manager (use SessionStore instead)
    - ``AuthConfig``       — legacy config (use Credentials + AuthFactory)

Usage (new)::

    from crawler.auth import AuthFactory, SessionStore

    handler = AuthFactory.detect("https://me.sap.com/home")
    if handler:
        store = SessionStore(handler=handler)
        context = await store.get_authenticated_context(browser, portal_url=url)

Usage (legacy — still works)::

    from crawler.auth import AuthConfig, SessionManager

    auth_cfg = AuthConfig(login_url="...", username="...", password="...")
    session = SessionManager(auth_cfg)
"""

# ── New modular architecture ──────────────────────────────────────
from .base_auth import BaseAuthHandler, Credentials
from .auth_factory import AuthFactory
from .session_store import SessionStore
from .sap_auth import SAPAuthHandler

# ── Legacy compatibility ──────────────────────────────────────────
from .login_manager import LoginManager
from .sap_handler import SAPLoginHandler
from .session_manager import SessionManager, AuthConfig
from .session_bootstrap import bootstrap_session

__all__ = [
    # New modular API
    "BaseAuthHandler",
    "Credentials",
    "AuthFactory",
    "SessionStore",
    "SAPAuthHandler",
    # Legacy (backward-compatible)
    "LoginManager",
    "SAPLoginHandler",
    "SessionManager",
    "AuthConfig",
    "bootstrap_session",
]
