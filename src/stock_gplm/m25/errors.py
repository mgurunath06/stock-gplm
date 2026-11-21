"""Stable M2.5 failure categories for monitoring and CLI exit handling."""


class AuthError(RuntimeError):
    classification = "AUTH_FAILED"


class LoginRequired(AuthError):
    classification = "LOGIN_REQUIRED"


class LoginBlocked(AuthError):
    """Automatic browser login paused after failures (2026-11-21): protects the account from repeated submissions."""
    classification = "LOGIN_BLOCKED"


class ValidationInconclusive(AuthError):
    classification = "VALIDATION_INCONCLUSIVE"


class InteractionRequired(AuthError):
    classification = "INTERACTION_REQUIRED"


class UiContractChanged(AuthError):
    classification = "UI_CONTRACT_CHANGED"


class TokenExchangeRejected(AuthError):
    classification = "TOKEN_EXCHANGE_REJECTED"


class SecurityBoundaryError(AuthError):
    classification = "SECURITY_BOUNDARY_ERROR"


class BrowserPageNotReady(AuthError):
    classification = "BROWSER_PAGE_NOT_READY"
