"""The enterprise build's one sign-in (RFC D4): Crew's own vault sign-in.

The vault is a real ``TokenStore`` in a temporary directory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import enterprise_sign_in as sign_in
from kiro_crew.auth.store import KasToken, TokenStore

_ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABCDEFGHIJKL"
_OTHER_ARN = "arn:aws:codewhisperer:us-east-1:210987654321:profile/ZYXWVUTSRQPO"


@pytest.fixture(name="store")
def store_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    store = TokenStore(tmp_path / "vault")
    monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: store)
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    return store


@pytest.fixture(name="vault")
def vault_fixture(store: TokenStore):
    def save(
        profile_arn: str | None = _ARN,
        *,
        auth_method: str = "identity_center",
        expires_in: float = 3600,
        refresh_token: str | None = "refresh",
    ) -> None:
        store.save(
            KasToken(
                access_token="vault-access",
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                provider="Enterprise",
                identity=auth_method,
                refresh_token=refresh_token,
                profile_arn=profile_arn,
                auth_method=auth_method,
                token_endpoint="https://idp.example.invalid/token",
            )
        )

    return save


@pytest.fixture(name="unpinned")
def unpinned_fixture(monkeypatch: pytest.MonkeyPatch):
    from kiro_crew.platform import enterprise_policy_fetch as epf

    monkeypatch.setattr(epf, "_pinned", "")
    monkeypatch.delenv(epf.PROFILE_PIN_ENV, raising=False)
    monkeypatch.setattr(epf, "cache_only_process", lambda: False)
    return epf


def _cause(**kwargs) -> str:
    with pytest.raises(sign_in.SignInError) as exc:
        sign_in.resolve(**kwargs)
    return exc.value.cause


class TestSelection:
    def test_a_vault_sign_in_is_the_sign_in(self, vault) -> None:
        vault()
        found = sign_in.resolve()
        assert (found.source, found.profile_arn, found.token) == (sign_in.Source.VAULT, _ARN, "")

    def test_the_token_read_returns_the_vault_token(self, vault) -> None:
        vault()
        found = sign_in.resolve(with_token=True)
        assert (found.token, found.profile_arn, found.token_type) == ("vault-access", _ARN, "")

    def test_an_external_idp_sign_in_sends_its_token_type(self, vault) -> None:
        vault(auth_method="external_idp")
        assert sign_in.resolve(with_token=True).token_type == sign_in.TOKEN_TYPE_EXTERNAL_IDP

    def test_an_empty_vault_is_not_signed_in(self, store) -> None:
        with pytest.raises(sign_in.SignInError) as exc:
            sign_in.resolve()
        assert exc.value.cause == sign_in.CAUSE_NO_SIGN_IN
        assert "kirocrew kas login --provider idc" in str(exc.value)

    def test_no_profile_is_not_an_organisation_account(self, vault) -> None:
        vault(None, auth_method="external_idp")
        assert _cause() == sign_in.CAUSE_NOT_ELIGIBLE

    def test_an_unreadable_vault_fails_closed(self, monkeypatch) -> None:
        class Broken:
            def holds_no_identity(self) -> bool:
                raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: Broken())
        assert _cause() == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_the_result_is_frozen_and_never_prints_the_token(self, vault) -> None:
        vault()
        found = sign_in.resolve(with_token=True)
        assert "vault-access" not in repr(found)
        with pytest.raises(AttributeError):
            found.profile_arn = _OTHER_ARN  # type: ignore[misc]

    def test_no_kiro_cli_store_is_read(self, store) -> None:
        # Vault only in this change; the kiro-cli fallback lands in a follow-up.
        assert list(sign_in.Source) == [sign_in.Source.VAULT]
        assert not any("kiro_cli" in name.lower() for name in vars(sign_in))


class TestLapsed:
    def test_a_lapsed_vault_is_not_signed_in_but_names_its_account(self, vault) -> None:
        vault(expires_in=-3600, refresh_token=None, auth_method="external_idp")
        chosen = sign_in.select()
        assert chosen.profile_arn == _ARN and chosen.problem is not None
        with pytest.raises(sign_in.SignInError) as exc:
            sign_in.resolve(with_token=True)
        assert exc.value.cause == sign_in.CAUSE_SIGN_IN_UNUSABLE
        assert "kirocrew kas login --provider idc" in str(exc.value)

    def test_a_refused_refresh_is_unusable(self, vault, store) -> None:
        vault(expires_in=-3600)
        store.mark_refresh_rejected("identity_center")
        assert _cause() == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_an_expired_token_not_yet_refused_still_counts(self, vault) -> None:
        vault(expires_in=-3600)
        assert sign_in.resolve().source == sign_in.Source.VAULT


class TestAccountLock:
    def test_a_lapsed_sign_in_is_not_an_account_change(self, vault, unpinned) -> None:
        epf = unpinned
        vault()
        pin = epf.pinned_scope()
        vault(expires_in=-3600, refresh_token=None, auth_method="external_idp")
        assert epf.signed_in_scope() == pin
        epf.assert_signed_in_unchanged()  # does not raise, does not stop the gateway
        with pytest.raises(epf.EnterprisePolicyError) as exc:
            epf.signed_in_identity()
        assert exc.value.cause == sign_in.CAUSE_SIGN_IN_UNUSABLE

    def test_an_unreadable_vault_is_not_an_account_change(
        self, vault, unpinned, monkeypatch
    ) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        epf = unpinned
        stops: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.platform.policy_distribution.central_ceiling_installed", lambda: True
        )
        monkeypatch.setattr(epf.signal, "raise_signal", lambda sig: stops.append(sig))
        vault()
        epf.pinned_scope()

        class Broken:
            def holds_no_identity(self) -> bool:
                raise OSError("unreadable")

        monkeypatch.setattr("kiro_crew.auth.bridge.default_token_store", lambda: Broken())
        with pytest.raises(PlatformCompositionError) as exc:
            epf.assert_signed_in_unchanged()
        assert epf.ACCOUNT_CHANGED not in str(exc.value)
        assert stops == []

    def test_a_never_pinned_start_is_never_called_an_account_change(
        self, vault, unpinned, monkeypatch
    ) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        epf = unpinned
        monkeypatch.setattr(epf, "signed_in_scope", lambda: "")  # pin read raced a sign-in
        vault()
        with pytest.raises(PlatformCompositionError) as exc:
            epf.assert_signed_in_unchanged()
        assert epf.ACCOUNT_CHANGED not in str(exc.value)

    def test_the_lock_and_the_fetch_name_the_same_account(self, vault) -> None:
        from kiro_crew.platform import enterprise_policy_fetch as epf

        vault()
        assert epf.signed_in_scope() == epf.profile_scope(epf.signed_in_identity().profile_arn)


class TestLoginSource:
    @pytest.mark.asyncio
    async def test_it_follows_a_sign_out(self, vault, store) -> None:
        vault()
        source = sign_in.SignInLoginSource()
        assert source.authenticated
        store.delete("identity_center")
        assert await source.login_verified(max_age_secs=0) is False
        assert not source.authenticated

    @pytest.mark.asyncio
    async def test_a_refused_refresh_closes_the_gate(self, vault, store) -> None:
        vault(expires_in=-3600)
        source = sign_in.SignInLoginSource()
        assert source.authenticated
        store.mark_refresh_rejected("identity_center")
        assert await source.login_verified(max_age_secs=0) is False

    @pytest.mark.asyncio
    async def test_an_account_switch_closes_the_gate(self, vault, unpinned) -> None:
        vault(_ARN)
        source = sign_in.SignInLoginSource()
        assert source.authenticated  # pins _ARN
        vault(_OTHER_ARN)
        assert await source.login_verified(max_age_secs=0) is False

    def test_describe_reports_the_sign_in(self, vault) -> None:
        vault()
        line = sign_in.describe()
        assert line.startswith("✅") and "Kiro Crew's own sign-in" in line and _ARN in line

    def test_describe_reports_no_sign_in(self, store) -> None:
        assert sign_in.describe().startswith("❌ no Kiro sign-in was found")


class TestDoctor:
    def test_an_enterprise_doctor_reports_the_sign_in(self, vault, monkeypatch, capsys) -> None:
        from kiro_crew import cli_doctor

        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        vault()
        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        out = capsys.readouterr().out
        assert "sign-in:" in out and "Kiro Crew's own sign-in" in out and _ARN in out
        assert issues == []

    def test_an_enterprise_doctor_with_no_sign_in_is_an_issue(
        self, store, monkeypatch, capsys
    ) -> None:
        from kiro_crew import cli_doctor

        monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")
        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        assert "no Kiro sign-in was found" in capsys.readouterr().out
        assert issues and issues[0].startswith("enterprise sign-in: no Kiro sign-in")

    def test_an_oss_doctor_prints_no_sign_in_row(self, store, capsys) -> None:
        from kiro_crew import cli_doctor

        issues: list[str] = []
        cli_doctor._doctor_enterprise_sign_in(issues)
        assert capsys.readouterr().out == "" and issues == []


class TestEditionFree:
    def test_the_resolver_does_not_read_the_edition(self) -> None:
        """Callers decide when it applies, so an oss caller can reuse it as-is."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sign_in))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert "edition" not in names and "kiro_crew.edition" not in names

    def test_the_resolver_answers_in_an_oss_build(self, vault) -> None:
        vault()
        assert sign_in.select().source == sign_in.Source.VAULT  # EDITION is oss here

    @pytest.mark.asyncio
    async def test_the_gate_wiring_builds_no_source_in_oss(self) -> None:
        from kiro_crew.dashboard.enterprise_gate_middleware import enterprise_login_source

        assert await enterprise_login_source() is None
