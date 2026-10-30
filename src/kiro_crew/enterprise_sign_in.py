"""The enterprise build's one sign-in: who is signed in, for every enterprise check.

Design: ``docs/request-for-change/rfc-crew-enterprise-edition.md``, D4. The login check
at start (:func:`kiro_crew.enterprise_boot.require_kiro_login`), the gate
(:class:`SignInLoginSource`), the rules fetch
(:mod:`kiro_crew.platform.enterprise_policy_fetch`) and the account lock (its profile
pin) all read :func:`select`, so they can never name different accounts.

Today one source is accepted: Crew's own vault (``kirocrew kas login``, the dashboard's
Sign in). A vault with no entry is "not signed in"; a lapsed entry (one that can no
longer produce a token) is also "not signed in", but it still names its account, so
the account lock never reads it as a change of account. :class:`Source` names the
source a :class:`Selection` came from, so a second source can be added to
:func:`select` without changing any reader.

Every refusal is a :class:`SignInError` carrying one of the ``CAUSE_*`` values, so the
boot error picks its fix from the real cause.

This module does not read the edition. Its callers decide when it applies: today only
the enterprise wiring calls it (``enterprise_boot``, the enterprise gate, the enterprise
policy fetch and doctor's enterprise row), so nothing here runs in an ``oss`` build.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class Source(str, enum.Enum):
    """Where a sign-in came from."""

    VAULT = "vault"


#: What to call each source in front of a person.
SOURCE_LABELS = {
    Source.VAULT: "Kiro Crew's own sign-in",
}

VAULT_SIGN_IN_COMMAND = "kirocrew kas login --provider idc --start-url <your start URL>"
#: How to sign in, for a message that has found no usable sign-in.
SIGN_IN = f"`{VAULT_SIGN_IN_COMMAND}`"

# The real cause of a refused enterprise start. Read by enterprise_boot to pick the fix.
CAUSE_NO_SIGN_IN = "no_sign_in"
CAUSE_SIGN_IN_UNUSABLE = "sign_in_unusable"
CAUSE_UNREACHABLE = "unreachable"
CAUSE_REFUSED = "refused"
CAUSE_NOT_ELIGIBLE = "not_eligible"
CAUSE_ACCOUNT_CHANGED = "account_changed"
CAUSE_BAD_RULES = "bad_rules"

TOKEN_TYPE_EXTERNAL_IDP = "EXTERNAL_IDP"

#: Seconds to wait for the vault read and token refresh.
_CREDENTIAL_TIMEOUT_SECS = 30.0
#: The oldest a gate check may be before the next one re-reads the store.
_MAX_CHECK_AGE_SECS = 60.0


class SignInError(RuntimeError):
    """No usable sign-in. ``str()`` is safe to print; ``cause`` is a ``CAUSE_*`` value."""

    def __init__(self, message: str, cause: str) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class Selection:
    """The one answer every check reads: which source, which account, and its token.

    ``source`` and ``profile_arn`` come from what is STORED (presence, never
    liveness). ``problem`` is set when the stored sign-in is lapsed (it cannot produce
    a token): the account is still known, but nothing may use it. ``token`` is empty
    unless the caller asked for one; it is never printed.
    """

    source: Source
    profile_arn: str
    problem: Optional[SignInError] = None
    token: str = ""
    token_type: str = ""

    def __repr__(self) -> str:
        return (
            f"Selection(source={self.source.value!r}, profile_arn={self.profile_arn!r}, "
            f"problem={self.problem!r}, token=<redacted>)"
        )

    @property
    def label(self) -> str:
        return SOURCE_LABELS[self.source]


def run_coroutine(coro: Any, *, timeout: float = _CREDENTIAL_TIMEOUT_SECS) -> Any:
    """Run *coro* to completion on its own loop in a worker thread.

    Callers are synchronous code that may itself be running on an event loop thread
    (boot) or on the policy refresher's thread, so running a loop here directly could
    find one already running.
    """
    box: Dict[str, Any] = {}

    def work() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc
        finally:
            loop.close()

    worker = threading.Thread(target=work, name="enterprise-sign-in-credential", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("the Kiro sign-in did not answer in time")
    if "error" in box:
        raise box["error"]
    return box["value"]


# ── messages ────────────────────────────────────────────────────────────────


def _no_sign_in_message() -> str:
    return f"no Kiro sign-in was found; sign in with {SIGN_IN}"


def _no_profile_message() -> str:
    return (
        f"{SOURCE_LABELS[Source.VAULT]} has no profile, so it is not an organisation "
        f"account; sign in with an organisation account: {SIGN_IN}"
    )


def _unusable(detail: str) -> SignInError:
    return SignInError(
        f"{SOURCE_LABELS[Source.VAULT]} {detail}; sign in again with {SIGN_IN}",
        CAUSE_SIGN_IN_UNUSABLE,
    )


# ── Crew's vault ────────────────────────────────────────────────────────────


def _vault_entry() -> Any:
    """The vault's stored identity, ``None`` when it holds none, or raise.

    Presence, not liveness: a lapsed entry is still returned. A vault that cannot be
    read, or holds an entry that will not decrypt, raises (fail closed), so it is never
    read as "no sign-in".
    """
    try:
        from kiro_crew.auth.bridge import default_token_store

        store = default_token_store()
        if store.holds_no_identity():
            return None
        token = store.resolve()
    except Exception:
        logger.warning("Kiro Crew sign-in vault could not be read", exc_info=True)
        raise _unusable("could not be read") from None
    if token is None:
        raise _unusable("could not be read")
    return token


def _vault_rejected(token: Any) -> bool:
    """Is *token* expired with a refresh token the issuer has already refused?

    ``is_usable`` cannot know that without a network call; the refresher records the
    refusal (``TokenStore.refresh_rejected``), and saving a new sign-in clears it.
    """
    if not token.is_expired():
        return False
    try:
        from kiro_crew.auth.bridge import default_token_store

        return default_token_store().refresh_rejected(token.identity) is not None
    except Exception:
        logger.warning("Kiro Crew sign-in refusal marker could not be read", exc_info=True)
        return True


def _vault_token(expected_arn: str) -> Selection:
    """The vault's sign-in with a live access token, refreshed if due, or raise."""
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated

    provider = KasAuthProvider(default_token_store(), allow_env_api_key=False)
    try:
        token = run_coroutine(provider.current())
    except NotAuthenticated:
        raise SignInError(_no_sign_in_message(), CAUSE_NO_SIGN_IN) from None
    except Exception as exc:
        raise _unusable(f"could not be read or refreshed ({type(exc).__name__})") from None
    if not token.profile_arn:
        raise SignInError(_no_profile_message(), CAUSE_NOT_ELIGIBLE)
    if token.profile_arn != expected_arn:
        # The vault was rewritten between the selection and the refresh.
        raise _unusable("changed while it was being read")
    token_type = TOKEN_TYPE_EXTERNAL_IDP if token.auth_method == "external_idp" else ""
    return Selection(Source.VAULT, token.profile_arn, None, token.access_token, token_type)


# ── the resolver ────────────────────────────────────────────────────────────


def select() -> Selection:
    """The one enterprise sign-in, by presence, or raise :class:`SignInError`.

    Raises for no sign-in, a store that cannot be read, and a sign-in with no profile.
    A lapsed sign-in is returned with ``problem`` set. A plain store read: no refresh,
    no network, no spawn.
    """
    vault = _vault_entry()
    if vault is None:
        raise SignInError(_no_sign_in_message(), CAUSE_NO_SIGN_IN)
    if not vault.profile_arn:
        raise SignInError(_no_profile_message(), CAUSE_NOT_ELIGIBLE)
    problem = None
    if not vault.is_usable() or _vault_rejected(vault):
        problem = _unusable("has expired and cannot be renewed")
    return Selection(Source.VAULT, vault.profile_arn, problem)


def resolve(*, with_token: bool = False) -> Selection:
    """The selected sign-in (:func:`select`) when it is usable, or raise.

    *with_token* also returns a live access token, for the rules fetch: refreshed if
    due, from the same vault entry the selection named.
    """
    chosen = select()
    if chosen.problem is not None:
        raise chosen.problem
    if not with_token:
        return chosen
    return _vault_token(chosen.profile_arn)


def describe() -> str:
    """One line for ``kirocrew doctor``: which sign-in an enterprise build uses."""
    try:
        sign_in = resolve()
    except SignInError as exc:
        return f"❌ {exc}"
    except Exception as exc:  # doctor must not crash on a broken store
        return f"⚠  could not check ({type(exc).__name__})"
    return f"✅ {sign_in.label} ({sign_in.profile_arn})"


class SignInLoginSource:
    """The enterprise gate's login source (``enterprise_gate.LoginSource``), from :func:`resolve`.

    ``authenticated`` is the latched verdict of the last check. ``login_verified``
    re-checks when the last check is older than *max_age_secs*. Each check is a plain
    store read, run off the event loop.
    """

    def __init__(self) -> None:
        self._ok = self._check()
        self._at = time.monotonic()

    @staticmethod
    def _check() -> bool:
        """Signed in, and (once the account lock has pinned) as the pinned account."""
        try:
            found = resolve()
            from kiro_crew.platform import enterprise_policy_fetch as epf

            pin = epf.pinned_scope()
            if pin and epf.profile_scope(found.profile_arn) != pin:
                logger.warning("Enterprise sign-in check: %s", epf.ACCOUNT_CHANGED)
                return False
        except SignInError as exc:
            logger.warning("Enterprise sign-in check: %s", exc)
            return False
        except Exception:
            logger.warning("Enterprise sign-in check raised", exc_info=True)
            return False
        return True

    @property
    def authenticated(self) -> bool:
        return self._ok

    async def login_verified(self, *, max_age_secs: float) -> bool:
        # A plain store read is cheap, so a sign-out is seen within a minute even
        # while the watch's own budget for a re-check is longer.
        if time.monotonic() - self._at >= min(max_age_secs, _MAX_CHECK_AGE_SECS):
            self._ok = await asyncio.to_thread(self._check)
            self._at = time.monotonic()
        return self._ok
