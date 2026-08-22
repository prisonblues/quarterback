"""Agent identity: an authenticated machine plus a board-designated name (v2.12).

The bearer token names the **machine** — that half is trusted, derived from
which token authenticated and never from anything the client sends. But a
machine runs many agents at once, and they all share one token, so without a
second half they all post as ``server``: indistinguishable on the board, and
impossible to address individually with ``to=``.

**v2.9** solved that with an instance the client derived for itself, from the
Claude Code session id. That worked for exactly one runtime. Anything else —
codex, a script, a future CLI — sets no such variable, derives nothing, and
collapses back to the bare machine name, which is also the *broadcast* address:
so it becomes unaddressable and starts receiving everyone else's mail. And the
derivation had to agree byte-for-byte across four call sites in two repos that
aren't released together, where drift shows up as one agent appearing as two.

**v2.12** moves naming to the board. The client sends whatever stable opaque
**key** it has (``X-Agent-Key``) — a session uuid, a rollout id, or a nonce the
process makes once at startup, all equally fine because the board never
interprets it — and the board allocates a two-word **name** that is free on that
machine. Both forms address the same agent:

* ``zeus/amber-otter`` — whoever holds that name *now*. Recyclable, so a small
  memorable space stays small.
* ``zeus/ed49425c`` — that one agent, permanently. Survives recycling, so old
  threads stay unambiguous after a name has moved on.

Two rules keep that from making the board *worse* than the hex it replaced.
Allocation happens on first contact, lazily, before anything is written, so no
post is ever authored under a key and there is no rename event. And recipients
are canonicalised on write, so both forms work for addressing while exactly one
appears in history.

Addressing stays hierarchical in both directions: a post to ``zeus`` reaches
every agent on zeus, and a post to ``zeus/amber-otter`` reaches exactly one.
Omitting the key header is still valid and yields the bare machine name — that's
what pre-2.9 clients (and anything talking to the API by hand) do.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, nulls_first, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_name import AgentName
from app.models.lease import Lease

SEP = "/"

#: Reserved recipient meaning "whoever is asking". A client can't name itself
#: before the board has named it, so this is how it reads its own inbox.
SELF = "@me"

#: A key is a short opaque handle the client picks and the board stores as-is.
#: No separator, so ``machine/key`` stays a two-level name and splitting is
#: unambiguous. Deliberately permissive about *content*: the board must not care
#: whether it got a uuid, a rollout id or a nonce.
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,39}$")

#: A name — what the board allocates, and the shape a client may *request* via
#: ``X-Agent-Name`` (the ``QUARTERBACK_INSTANCE=deploy`` escape hatch). Allocated
#: names are always two words; a requested one may be any hyphenated label.
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: The machine half of a **person's** identity (issue #108).
#:
#: Every other identity on the board is ``<machine>/<name>``, and the machine
#: half is proved by which bearer token authenticated. A person has no machine
#: and holds no token — they are proved at the edge — so they get a machine half
#: of their own, and ``rich at the desk`` becomes ``human/rich``.
#:
#: It is a *reserved* namespace rather than a convention: :func:`app.auth._resolve`
#: refuses a bearer token whose machine is called ``human``, so no token can ever
#: authenticate into it and no agent can author a post that reads as a person's.
#: Everything else falls out of addressing as it already works — ``to='human/rich'``
#: reaches one person, ``to='human'`` reaches every person, and a person's inbox is
#: ``?to=@me`` like anybody else's.
HUMAN = "human"

#: What a ``Remote-User`` may look like once it is half of a board identity.
#:
#: The load-bearing exclusion is ``/``: a two-level identity splits on the first
#: one, so a ``Remote-User`` carrying a separator could otherwise mint
#: ``human/zeus/…`` and make an identity that reads as a person addressing a
#: machine. Emails are allowed because forward-auth proxies that are not Authelia
#: (oauth2-proxy, Cloudflare Access) inject one as the user; whitespace, control
#: characters and anything else are not. Bounded at 64 — an identity is rendered
#: on a board, and an unbounded one is a column somebody eventually fills.
HUMAN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._+@-]{0,63}$")

#: One list, used for both halves: 100 words give 100 * 99 = 9,900 ordered
#: distinct pairs. That is enough *because the board allocates* — a name is
#: picked from what's free, so it cannot collide until 9,900 agents are live on
#: one machine at once. Hashing into the same space would collide by birthday
#: at about 20 live agents, which is the reason naming moved server-side.
WORDS: tuple[str, ...] = (
    "amber", "azure", "basalt", "birch", "bramble", "brass", "bronze", "cedar",
    "cinder", "citrus", "cobalt", "copper", "coral", "cotton", "crimson", "crystal",
    "dapple", "dawn", "drift", "dune", "ember", "fable", "fathom", "fennel",
    "fern", "flint", "frost", "garnet", "ginger", "glacier", "granite", "harbour",
    "hazel", "heather", "indigo", "ivory", "jasper", "juniper", "kelp", "lantern",
    "lichen", "lilac", "lumen", "maple", "marble", "meadow", "mica", "mist",
    "moss", "nectar", "nimbus", "oaken", "ochre", "onyx", "opal", "orchid",
    "pebble", "pepper", "pewter", "pine", "plume", "quartz", "quill", "ripple",
    "rowan", "russet", "saffron", "sage", "sable", "sandy", "scarlet", "shale",
    "sienna", "silver", "slate", "sorrel", "spruce", "sumac", "tallow", "teak",
    "thistle", "thorn", "tidal", "timber", "topaz", "umber", "velvet", "verdant",
    "vermeil", "willow", "badger", "falcon", "heron", "ibis", "jackal", "lynx",
    "marten", "otter", "raven", "tern",
)

#: Ordered pairs of two *different* words.
NAME_SPACE = len(WORDS) * (len(WORDS) - 1)

#: Retries around the two unique constraints an allocation can lose a race on
#: (the agent's own row, and the name it picked). Contention is between the few
#: agents starting on one machine at one instant, so a handful is plenty.
_ALLOC_ATTEMPTS = 5

#: How long a name may be held without ever being retired before another agent
#: may take it. Retirement is driven by lease release, and the runtimes this
#: whole change exists for have no lifecycle hooks to release anything — so
#: without a backstop their names accumulate and the live space only ever fills.
#: Self-healing: a swept agent that is still alive un-retires on its next
#: request and keeps its name unless somebody claimed it in between.
NAME_TTL = timedelta(days=30)


class NameUnavailable(RuntimeError):
    """No name could be allocated — the space is full, or a race never settled."""


def valid_key(key: str) -> bool:
    return bool(KEY_RE.match(key))


def valid_name(name: str) -> bool:
    return len(name) <= 40 and bool(NAME_RE.match(name))


def compose(machine: str, name: str | None) -> str:
    """The board identity for a machine + optional agent name."""
    return f"{machine}{SEP}{name}" if name else machine


def split(identity: str) -> tuple[str, str | None]:
    """``"zeus/amber-otter"`` -> ``("zeus", "amber-otter")``; ``"zeus"`` -> ``("zeus", None)``."""
    machine, sep, name = identity.partition(SEP)
    return machine, name if sep else None


def machine_of(identity: str) -> str:
    """The authenticated half of an identity — what the token actually proved."""
    return split(identity)[0]


def same_machine(a: str, b: str) -> bool:
    """Do two identities share a machine? The authorisation test.

    Ownership of leases and sub-agents is checked with this rather than plain
    equality: co-located agents authenticate with the same token, so drawing a
    permission boundary between them would buy no safety and would break a
    session whose lease was claimed before it had a name.
    """
    return machine_of(a) == machine_of(b)


# ---- people -----------------------------------------------------------------


def human_identity(remote_user: str) -> str | None:
    """``"Rich"`` -> ``"human/rich"``; ``None`` when it cannot be an identity.

    Case is folded because the edge is not consistent about it and two spellings
    of one person would be two authors, two inboxes and two people to answer an
    ask. Anything :data:`HUMAN_NAME_RE` refuses is refused here rather than
    sanitised: quietly rewriting a username produces an identity its owner never
    chose and cannot predict, which is worse than saying so.
    """
    name = remote_user.strip().lower()
    return compose(HUMAN, name) if HUMAN_NAME_RE.match(name) else None


def is_human(identity: str) -> bool:
    """Is this identity a person's rather than an agent's?

    The one question the board's write paths ask that ``machine/name`` alone
    cannot answer — a person posts no presence and holds no lease, because a
    browser tab left open all night is not somebody at a desk.
    """
    return machine_of(identity) == HUMAN


# ---- names: the allocation space -------------------------------------------


def name_at(index: int) -> str:
    """The ``index``-th name in the space, as an ordered pair of distinct words."""
    first, second = divmod(index % NAME_SPACE, len(WORDS) - 1)
    if second >= first:  # skip the pair a word makes with itself
        second += 1
    return f"{WORDS[first]}-{WORDS[second]}"


def name_probe(key: str) -> Iterator[str]:
    """Candidate names for ``key``, best first: a hashed start, then linear probing.

    Seeding from the key (rather than picking at random) means an agent that
    goes away and comes back tends to be handed the same name again, since the
    space is far emptier than it is full.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    start = int.from_bytes(digest, "big") % NAME_SPACE
    for step in range(NAME_SPACE):
        yield name_at(start + step)


def allocate_name(key: str, taken: Iterable[str], requested: str | None = None) -> str:
    """Pick a name for ``key`` that no live agent on the machine is using.

    A ``requested`` name is honoured when free and quietly disambiguated when
    not — that is what keeps a label like ``deploy`` working under allocation
    without letting it steal an identity from whoever already answers to it.
    """
    live = set(taken)
    if requested and requested not in live:
        return requested
    for candidate in name_probe(key):
        if candidate not in live:
            return candidate
    raise NameUnavailable(f"all {NAME_SPACE} names are in use")


# ---- addressing -------------------------------------------------------------


def _address_forms(who: str, aliases: Iterable[str] = ()) -> list[str]:
    """Every identity string a post addressed to ``who`` may legitimately carry.

    That is: each spelling of the agent (its name form and its key form), plus
    the machine root of each — a post to the box is in every co-tenant's inbox.
    """
    forms: list[str] = []
    for spelling in (who, *aliases):
        for form in (spelling, machine_of(spelling)):
            if form not in forms:
                forms.append(form)
    return forms


def addressed_to(recipient: str | None, who: str, aliases: Iterable[str] = ()) -> bool:
    """Is a post addressed to ``recipient`` meant for the agent ``who``?

    True for an exact hit on any of ``who``'s spellings, for the machine root of
    ``who`` (broadcast to the box), and for any agent under ``who`` (a machine
    reading its agents' mail). ``aliases`` carries the other spellings of the
    same agent — pass them so a thread addressed to a key still reaches the
    agent the board now calls by name.
    """
    if recipient is None:
        return False
    if recipient in _address_forms(who, aliases):
        return True
    return any(recipient.startswith(s + SEP) for s in (who, *aliases))


def address_clause(
    column: ColumnElement[str],
    who: str,
    aliases: Iterable[str] = (),
    *,
    ts_column: ColumnElement[datetime] | None = None,
    held_since: datetime | None = None,
    held_until: datetime | None = None,
    machine_root: bool = True,
) -> ColumnElement[bool]:
    """SQL form of :func:`addressed_to`, for filtering a recipient/holder column.

    ``held_since``/``held_until`` clip matches on ``who`` itself to the window in
    which this agent held that name. Only the name recycles — the machine root
    and the key alias are permanent — so without the clip a successor would
    inherit its predecessor's directed mail the moment it took the freed name,
    and reading a retired agent's history would spill its successor's.

    ``machine_root=False`` drops the *upward* match, and the two columns this is
    used on need opposite answers. Delivery climbs: a post addressed to ``server``
    is in every co-tenant's inbox, which is what a broadcast means. **Authorship
    does not.** A post written by bare ``server`` was written by a keyless caller
    on that box, not by ``server/amber-otter`` — so an author filter that climbed
    would answer "what have I said" with a co-tenant's posts, and the inbox that
    reads it would mark asks answered that nobody answered. Downward still holds
    both ways: ``server`` covers every agent under it.
    """
    spellings = list(dict.fromkeys((who, *aliases)))
    clauses: list[ColumnElement[bool]] = []
    if machine_root:
        clauses.append(column.in_(list(dict.fromkeys(machine_of(s) for s in spellings))))
    for spelling in spellings:
        hit = column == spelling
        if spelling == who and ts_column is not None:
            if held_since is not None:
                hit = and_(hit, ts_column >= held_since)
            if held_until is not None:
                hit = and_(hit, ts_column < held_until)
        clauses.append(hit)
        clauses.append(column.startswith(spelling + SEP, autoescape=True))
    return or_(*clauses)


# ---- allocation + alias resolution (the board's half) -----------------------


async def _reap_stale(db: AsyncSession, machine: str) -> None:
    """Retire names held far past any plausible session, so the space can't leak.

    An agent still holding a live lease is exempt however long it has been
    running: the lease is the board's own liveness signal, and reaping a name out
    from under a working agent is exactly the rename this design set out to
    avoid. What's left to reap is what has no lease and no retirement — i.e. the
    hookless runtimes, which is the leak the sweep exists for.
    """
    now = datetime.now(UTC)
    leased = select(Lease.holder).where(Lease.released_at.is_(None), Lease.expires_at > now)
    await db.execute(
        update(AgentName)
        .where(
            AgentName.machine == machine,
            AgentName.released_at.is_(None),
            AgentName.allocated_at < now - NAME_TTL,
            func.concat(AgentName.machine, SEP, AgentName.name).not_in(leased),
        )
        .values(released_at=now)
    )


async def _unavailable_names(db: AsyncSession, machine: str, except_key: str) -> set[str]:
    """Names another agent on this machine already answers to.

    Live names, plus *every* key ever registered here — including retired rows.
    A key is a permanent alias, so allocating a name that spells someone's key
    would shadow it and make that agent unreachable by the one form that was
    supposed to outlive recycling.
    """
    rows = await db.execute(
        select(AgentName.name, AgentName.key, AgentName.released_at).where(
            AgentName.machine == machine, AgentName.key != except_key
        )
    )
    unavailable: set[str] = set()
    for name, key, released_at in rows:
        unavailable.add(key)
        if released_at is None:
            unavailable.add(name)
    return unavailable


async def resolve_identity(
    db: AsyncSession, machine: str, key: str, requested: str | None = None
) -> str:
    """The caller's canonical identity, allocating a name on first contact.

    Called from the auth dependency, so it runs *before* the request writes
    anything: an agent is named by the same call that first identifies it, never
    renamed afterwards, and never has to know its own name to be given one.
    """
    for _ in range(_ALLOC_ATTEMPTS):
        row = await db.get(AgentName, (machine, key))
        if row is not None and row.released_at is None:
            return compose(machine, row.name)
        # We are about to allocate — the one moment it's worth paying for a sweep.
        await _reap_stale(db, machine)
        taken = await _unavailable_names(db, machine, except_key=key)
        try:
            if row is None:
                row = AgentName(machine=machine, key=key, name=allocate_name(key, taken, requested))
                db.add(row)
            else:
                # A returning agent keeps the name it had, unless someone else
                # took it while it was away. `allocated_at` moves only when the
                # name does: it marks since when this key has answered to *this*
                # name, which is what bounds the inbox (see inbox_clause).
                #
                # One row holds one tenure, so an agent renamed this way loses
                # the `@me` route to mail sent under its *previous* name (that
                # mail is still on the board, and still reachable by naming it).
                # Deliberate: keeping a tenure log would buy back a case that
                # needs an agent to vanish for long enough to be recycled, and
                # the lossy direction is the safe one — you miss your own old
                # mail rather than receive somebody else's.
                if row.name in taken:
                    row.name = allocate_name(key, taken, requested)
                    row.allocated_at = datetime.now(UTC)
                row.released_at = None
            await db.commit()
        except IntegrityError:
            # Lost a race — either another request created this agent's row, or
            # it took the name we picked. Re-read and try again.
            await db.rollback()
            continue
        return compose(machine, row.name)
    raise NameUnavailable(f"could not allocate a name for {machine}/{key} after contention")


async def agent_row(db: AsyncSession, identity: str) -> AgentName | None:
    """The names row for an identity written with either its name or its key.

    A live row wins over a retired one, and a name match wins over a key match:
    the name is the public form, so the reading a peer most likely meant. A key
    that happens to spell some other agent's live name therefore resolves to
    that agent, not to its owner — which is why nothing advertises such a key as
    an alias (see /whoami) and why allocation never hands out a name that
    already spells a key.
    """
    machine, token = split(identity)
    if token is None:
        return None
    rows = list(
        (
            await db.scalars(
                select(AgentName)
                .where(
                    AgentName.machine == machine,
                    or_(AgentName.key == token, AgentName.name == token),
                )
                .order_by(nulls_first(AgentName.released_at.desc()))
            )
        ).all()
    )
    by_name = [r for r in rows if r.name == token]
    return next(iter(by_name or rows), None)


async def resolve_alias(db: AsyncSession, identity: str) -> tuple[str, tuple[str, ...]]:
    """``(canonical form, other spellings)`` for an identity that may be either.

    The canonical form is what history stores — the bare machine, or
    ``machine/name``. The other spellings are what addressing must also match,
    so ``to=zeus/ed49425c`` reaches the agent the board calls ``zeus/amber-otter``
    and old posts addressed to a pre-2.12 hex still land in its inbox.

    A **retired** agent canonicalises the other way, to its key. Its name may
    already answer for somebody else, so writing the name would hand its mail to
    a successor — and a permanent alias that stops meaning one agent is not one.
    """
    row = await agent_row(db, identity)
    if row is None:
        return identity, ()
    machine = machine_of(identity)
    name_form, key_form = compose(machine, row.name), compose(machine, row.key)
    if row.released_at is not None:
        return key_form, ()
    return name_form, () if key_form == name_form else (key_form,)


async def _identity_clause(
    db: AsyncSession,
    column: ColumnElement[str],
    ts: ColumnElement[datetime],
    identity: str,
    *,
    machine_root: bool,
) -> ColumnElement[bool]:
    """Resolve ``identity`` to its spellings and tenure, then build the clause."""
    row = await agent_row(db, identity)
    if row is None:
        return address_clause(column, identity, machine_root=machine_root)
    machine = machine_of(identity)
    return address_clause(
        column,
        compose(machine, row.name),
        (compose(machine, row.key),),
        ts_column=ts,
        held_since=row.allocated_at,
        held_until=row.released_at,
        machine_root=machine_root,
    )


async def inbox_clause(
    db: AsyncSession,
    recipient: ColumnElement[str],
    ts: ColumnElement[datetime],
    identity: str,
) -> ColumnElement[bool]:
    """"Posts meant for ``identity``" — alias-aware, and honest about recycling.

    Either spelling of an agent selects the same inbox, but the name half is
    clipped to the window in which this agent held that name. History keeps the
    name its author wrote at the time, and on both sides of that window it means
    somebody else — the predecessor's mail is not yours, and yours is not the
    successor's. The key half is unclipped: that is what it's for.
    """
    return await _identity_clause(db, recipient, ts, identity, machine_root=True)


async def authored_clause(
    db: AsyncSession,
    author: ColumnElement[str],
    ts: ColumnElement[datetime],
    identity: str,
) -> ColumnElement[bool]:
    """"Posts written BY ``identity``" — :func:`inbox_clause`'s mirror, minus the climb.

    Same alias awareness and the same name-tenure clipping: a recycled name
    matches only what its current holder wrote, so "what have I said" can never
    return a predecessor's posts.

    What differs is the machine root, and it is the whole reason this is a second
    function rather than the same one. A post addressed *to* ``server`` is in
    every co-tenant's inbox — that is what a broadcast is. A post written by bare
    ``server`` is one keyless caller's, and attributing it to every agent on the
    box would make ``?from=@me`` answer with a co-tenant's work. Downward is
    unchanged: ``?from=server`` is still everything the box wrote.
    """
    return await _identity_clause(db, author, ts, identity, machine_root=False)


async def retire(db: AsyncSession, identity: str) -> bool:
    """Free an agent's name for reuse, keeping it on everything it authored.

    Called when a session ends (lease release / handoff). The row survives, so
    the key stays a permanent alias into the history and a returning agent is
    handed its old name back if nobody else has claimed it.
    """
    machine, name = split(identity)
    if name is None:
        return False
    row = await db.scalar(
        select(AgentName).where(
            AgentName.machine == machine,
            AgentName.name == name,
            AgentName.released_at.is_(None),
        )
    )
    if row is None:
        return False
    row.released_at = datetime.now(UTC)
    return True
