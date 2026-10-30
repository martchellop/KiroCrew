"""Which files a KAS agent's ``file://`` prompt may be read from.

On KAS, Crew reads a ``file://`` prompt itself and ships the text over the wire
(:func:`kiro_crew.acp.kas_agents.resolve_prompt`), so the path a spec names
decides which file's contents reach the engine and the model. This module is the
one answer to "may this resolved path be inlined", and it is the same answer for
a relative path and an absolute one: a relative ``file://`` is anchored to the
agents directory and resolved, symlinks included, before it is asked.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.security import is_sensitive_path

#: Pseudo-filesystems whose contents are process/kernel state, not documents.
#: ``/proc/<pid>/environ`` is the sharp edge: the gateway's own environment.
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev")

#: Host secrets outside every home directory, which ``is_sensitive_path`` (a
#: ``$HOME``-relative fence) does not reach: the password hashes and the SSH host
#: private keys. A prompt file never lives under any of them. macOS resolves
#: ``/etc`` to ``/private/etc``, and the path asked about is resolved.
_HOST_SECRET_ROOTS = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/ssh",
    "/private/etc/ssh",
)

#: ``$HOME``-relative credential stores a prompt read must not reach that
#: ``is_sensitive_path`` leaves readable to the agent's own file tools. ``gh``
#: keeps its OAuth token in ``hosts.yml`` here. Fenced for this reader only: a
#: prompt is shipped off the host, a file the agent's tools read is not, so this
#: list widens nothing the sandbox or the tool gate decides.
_PROMPT_ONLY_HOME_SECRETS = (".config/gh",)


def _under(posix: str, root: str) -> bool:
    return posix == root or posix.startswith(root + "/")


def is_unsafe_prompt_path(path: Path) -> bool:
    """True if the RESOLVED *path* must not be read and inlined into a KAS prompt.

    *path* must already be resolved (``Path.resolve``), so a symlink is judged by
    its target. Blocks the credential and governance locations
    ``is_sensitive_path`` knows (which also re-resolves), the pseudo-filesystems,
    the host secrets in :data:`_HOST_SECRET_ROOTS` and the prompt-only credential
    stores in :data:`_PROMPT_ONLY_HOME_SECRETS`.
    """
    if is_sensitive_path(str(path)):
        return True
    posix = path.as_posix()
    if any(_under(posix, root) for root in _PSEUDO_FS_ROOTS + _HOST_SECRET_ROOTS):
        return True
    folded = posix.casefold()
    home = Path.home().resolve()
    return any(
        _under(folded, (home / rel).as_posix().casefold()) for rel in _PROMPT_ONLY_HOME_SECRETS
    )
