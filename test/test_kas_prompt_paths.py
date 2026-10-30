"""The KAS ``file://`` prompt fence: relative and absolute refs meet one check.

``resolve_prompt`` reads a ``file://`` prompt and ships the text over the wire, so
the RESOLVED path (symlinks followed) is what is judged. A relative ref may climb
out of the agents directory with ``..``, as on kiro-cli; it gains nothing an
absolute ref does not have, and both are refused at the same locations.

``HOME`` points at *tmp_path* so a relative ref can climb to a credential
location under it. The credential files are never created, except the symlink
target: the refusal is on the path, before any read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_crew.acp.kas_agents import KasAgentTranslationError, resolve_prompt
from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX home and /etc layout")

_HOME_CREDENTIALS = [".ssh/id_rsa", ".aws/credentials", ".config/gh/hosts.yml", ".netrc"]
_HOST_SECRETS = ["/etc/shadow", "/etc/gshadow", "/etc/ssh/ssh_host_ed25519_key"]


@pytest.fixture
def agents(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agents = home / ".kiro" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return agents


def _resolve(ref, agents):
    return resolve_prompt({"prompt": ref}, agent_id="a", agents_dir=agents)


@pytest.mark.parametrize("rel", _HOME_CREDENTIALS)
def test_a_relative_climb_to_a_credential_is_refused(agents, rel):
    with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
        _resolve(f"file://../../{rel}", agents)


@pytest.mark.parametrize("rel", _HOME_CREDENTIALS)
def test_the_same_credential_named_absolutely_is_refused(agents, rel):
    home = agents.parent.parent
    for ref in (f"file://~/{rel}", f"file://{home / rel}"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(ref, agents)


@pytest.mark.parametrize("target", _HOST_SECRETS)
def test_a_host_secret_is_refused_relative_or_absolute(agents, target):
    climb = "../" * len(agents.resolve().parts)
    for ref in (f"file://{target}", f"file://{climb}{target.lstrip('/')}"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(ref, agents)


@pytest.mark.parametrize("target", _HOST_SECRETS)
def test_each_added_host_secret_is_fenced(target):
    assert is_unsafe_prompt_path(Path(target))


def test_the_gh_token_store_is_fenced(agents):
    home = agents.parent.parent
    assert is_unsafe_prompt_path((home / ".config" / "gh" / "hosts.yml").resolve())
    assert not is_unsafe_prompt_path((home / ".config" / "ghostty" / "p.md").resolve())


def test_a_symlink_to_a_credential_is_refused(agents):
    home = agents.parent.parent
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("PRIVATE", encoding="utf-8")
    (agents / "prompt.md").symlink_to(home / ".ssh" / "id_rsa")
    for ref in ("file://./prompt.md", f"file://{agents / 'prompt.md'}"):
        with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
            _resolve(ref, agents)


def test_a_relative_climb_to_an_ordinary_file_loads(agents):
    shared = agents.parent / "prompts"
    shared.mkdir()
    (shared / "wizard.md").write_text("shared prompt", encoding="utf-8")
    assert _resolve("file://../prompts/wizard.md", agents) == "shared prompt"


def test_an_absolute_prompt_outside_home_still_loads(agents, tmp_path):
    p = tmp_path / "elsewhere" / "p.md"
    p.parent.mkdir()
    p.write_text("absolute", encoding="utf-8")
    assert _resolve(f"file://{p}", agents) == "absolute"
