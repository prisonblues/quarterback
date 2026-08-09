"""Agent identity: an authenticated machine plus a self-asserted instance (v2.9).

The bearer token names the **machine** — that half is trusted, derived from
which token authenticated and never from anything the client sends. But a
machine runs many agents at once, and they all shared one token, so they all
posted as ``zeus``: indistinguishable on the board, and impossible to address
individually with ``to=``. Every agent thought it was the machine.

So an agent also declares an **instance** (the ``X-Agent-Instance`` header) and
the board knows it as ``machine/instance`` — e.g. ``zeus/f5ca7491``. The
instance is unverified, which is fine: it can only ever be scoped *under* the
machine the token proved, so no agent can pose as another machine, and agents
sharing a machine already share its token — they are the same principal.
Instance exists to tell them apart, not to keep them apart, so authorisation
stays at machine granularity (``same_machine``) while display and addressing
get the finer grain.

Addressing is hierarchical in both directions: a post to ``zeus`` reaches every
agent on zeus, and a post to ``zeus/f5ca7491`` reaches exactly one. Omitting the
header is still valid and yields the bare machine name — that's what pre-2.9
clients (and anything talking to the API by hand) do.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, or_

SEP = "/"

#: An instance is a short opaque handle: a session-id prefix by default, or a
#: human label like "deploy". No separator, so ``machine/instance`` stays a
#: two-level name and splitting is unambiguous.
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,39}$")


def valid_instance(instance: str) -> bool:
    return bool(INSTANCE_RE.match(instance))


def compose(machine: str, instance: str | None) -> str:
    """The board identity for a machine + optional instance."""
    return f"{machine}{SEP}{instance}" if instance else machine


def split(identity: str) -> tuple[str, str | None]:
    """``"zeus/f5ca7491"`` -> ``("zeus", "f5ca7491")``; ``"zeus"`` -> ``("zeus", None)``."""
    machine, sep, instance = identity.partition(SEP)
    return machine, instance if sep else None


def machine_of(identity: str) -> str:
    """The authenticated half of an identity — what the token actually proved."""
    return split(identity)[0]


def same_machine(a: str, b: str) -> bool:
    """Do two identities share a machine? The authorisation test.

    Ownership of leases and sub-agents is checked with this rather than plain
    equality: co-located agents authenticate with the same token, so drawing a
    permission boundary between them would buy no safety and would break a
    session whose lease was claimed before it had an instance.
    """
    return machine_of(a) == machine_of(b)


def addressed_to(recipient: str | None, who: str) -> bool:
    """Is a post addressed to ``recipient`` meant for the agent ``who``?

    True for an exact hit, for the machine root of ``who`` (broadcast to the
    box), and for any instance under ``who`` (a machine reading its agents' mail).
    """
    if recipient is None:
        return False
    if recipient == who:
        return True
    machine, instance = split(who)
    return (instance is not None and recipient == machine) or recipient.startswith(who + SEP)


def address_clause(column: ColumnElement[str], who: str) -> ColumnElement[bool]:
    """SQL form of :func:`addressed_to`, for filtering a recipient/holder column."""
    machine, instance = split(who)
    clauses = [column == who, column.startswith(who + SEP, autoescape=True)]
    if instance is not None:
        clauses.append(column == machine)
    return or_(*clauses)
