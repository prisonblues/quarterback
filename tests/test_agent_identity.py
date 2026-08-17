"""v2.9: identity differentiation — machine/<agent> instead of just the machine.

Every agent on a box shares that box's token, so they all authored posts as
"server" and none could be addressed individually. The token still proves the
machine; a second half names the agent on it. These tests cover the composition,
the hierarchical addressing that falls out of it, and the fact that
authorisation deliberately did *not* get finer (co-tenants share a token, so a
permission boundary between them would be theatre).

v2.12 changed *who picks* that second half — the board designates it now, rather
than the client deriving it — so these tests no longer hard-code the agent half;
they ask ``/whoami`` for it. What v2.9 established is unchanged: two agents on
one machine are two identities, and a lease is still owned by the box.
See test_designated_names.py for the naming, aliasing and allocation that replaced it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.identity import (
    addressed_to,
    compose,
    machine_of,
    same_machine,
    split,
    valid_key,
    valid_name,
)

from .conftest import LAPTOP, SERVER

REPO = "v29repo"

# Two agents on one machine — the case that used to collapse to one identity.
SERVER_A = {**SERVER, "X-Agent-Key": "f5ca7491"}
SERVER_B = {**SERVER, "X-Agent-Key": "938fca68"}
# The requested name is incidental here — nothing below asserts it — but it has to be
# unique across the whole session, not just this file. A designated name is claimed once
# per machine, so a second file asking `laptop` for the same one is handed a different name
# and its assertion fails, in whichever of the two files happens to run second. That is what
# it did: this constant and `test_designated_names.py`'s NAMED both asked for `deploy`, and
# the collision was invisible for as long as the version-numbered filenames happened to sort
# the file that asserts on the name first. Renaming the files off their release numbers
# reordered collection and surfaced it — which is the argument for the rename in miniature.
#
# So the name is derived rather than chosen, and cannot be picked twice: the module owns
# exactly one filename, and no other module can spell it. Picking a second literal would only
# have moved the collision one file along, because nothing about a literal says it is free.
# The board validates a requested name (`valid_name`: lowercase, hyphen-separated, no
# underscore, no leading hyphen, at most forty characters), and module names are snake_case,
# hence the one substitution.
AGENT_NAME = Path(__file__).stem.replace("_", "-")
LAPTOP_A = {**LAPTOP, "X-Agent-Key": "deploykey", "X-Agent-Name": AGENT_NAME}


async def ident(client, headers) -> str:
    """The board identity behind a set of headers — no longer derivable locally."""
    return (await client.get("/whoami", headers=headers)).json()["agent"]


# ---- identity algebra (pure) ------------------------------------------------

def test_compose_and_split_round_trip():
    assert compose("server", "amber-otter") == "server/amber-otter"
    assert compose("server", None) == "server"
    assert split("server/amber-otter") == ("server", "amber-otter")
    assert split("server") == ("server", None)
    assert machine_of("server/amber-otter") == "server"


def test_same_machine_is_the_authorisation_grain():
    assert same_machine("server/amber-otter", "server/flint-raven")
    assert same_machine("server/amber-otter", "server")  # pre-2.9 holder vs. its agent
    assert not same_machine("server/amber-otter", "laptop/amber-otter")


def test_addressing_is_hierarchical_both_ways():
    # An agent's inbox: itself, plus anything sent to its whole machine.
    assert addressed_to("server/amber-otter", "server/amber-otter")
    assert addressed_to("server", "server/amber-otter")
    # A machine's inbox: anything sent to any of its agents.
    assert addressed_to("server/amber-otter", "server")
    # Never across machines, and never a mere prefix collision.
    assert not addressed_to("server/flint-raven", "server/amber-otter")
    assert not addressed_to("zeusling", "server")
    assert not addressed_to(None, "server")


def test_valid_key_rejects_separators_and_empties():
    assert valid_key("f5ca7491") and valid_key("deploy-2")
    assert not valid_key("has/slash")
    assert not valid_key("has space")
    assert not valid_key("")
    assert not valid_key("-leading")
    assert not valid_key("x" * 41)


# ---- the suite asking the board for names, seen from above ------------------

#: A machine constant as conftest spells them: LAPTOP, SERVER, DESKTOP.
MACHINE_CONST = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def _stem_derived(node: ast.expr, stem: str) -> str | None:
    """The value of ``Path(__file__).stem`` (plus any ``.replace``s) for a module, else None.

    A name derived from the filename is the shape this module uses, so it has to be
    resolvable or the module that motivated the whole check contributes nothing to it.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and len(node.args) == 2
        and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)
    ):
        base = _stem_derived(node.func.value, stem)
        old, new = (arg.value for arg in node.args if isinstance(arg, ast.Constant))
        return None if base is None else base.replace(old, new)
    if isinstance(node, ast.Attribute) and node.attr == "stem":
        call = node.value
        return stem if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "Path"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "__file__"
        ) else None
    return None


def _module_constants(tree: ast.Module, stem: str) -> tuple[dict[str, str], dict[str, ast.Dict]]:
    """Top-level ``NAME = <str>`` and ``NAME = {...}`` bindings, strings resolved."""
    strings: dict[str, str] = {}
    dicts: dict[str, ast.Dict] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            strings[target.id] = value.value
        elif (derived := _stem_derived(value, stem)) is not None:
            strings[target.id] = derived
        elif isinstance(value, ast.Dict):
            dicts[target.id] = value
    return strings, dicts


def _machines(node: ast.Dict, dicts: dict[str, ast.Dict], seen: frozenset[str]) -> set[str]:
    """The machine constants a header dict splats, following splats of local dicts."""
    found: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if key is not None:  # a splat is a None key
            continue
        if isinstance(value, ast.Dict):
            found |= _machines(value, dicts, seen)
        elif isinstance(value, ast.Name) and MACHINE_CONST.match(value.id):
            nested = value.id in dicts and value.id not in seen
            found |= (
                _machines(dicts[value.id], dicts, seen | {value.id}) if nested else set()
            ) or {value.id}
    return found


def requested_names(source: str, stem: str) -> set[tuple[str, str]]:
    """The ``(machine, name)`` pairs a test module asks the board to designate for it.

    A request is a dict literal that splats a machine constant and carries an
    ``X-Agent-Name`` key. The machine is part of the pair because names are claimed per
    machine, so two files may ask different boxes for the same label without ever meeting.

    This reads the parsed module rather than its text, so key order, quote style, padding,
    nesting depth and any braces in between are all irrelevant — they were the gaps in the
    regex this replaced, and each was a shape a future module could have written by accident.
    Values are resolved through top-level constants, including a name derived from the
    filename (this module's own shape), so a derived name and a literal spelling of the same
    string collide as they should.

    Out of scope, deliberately: a name the board handed out (``me["name"]``, an f-string, any
    call), which this suite did not choose and cannot claim twice; a literal ``valid_name``
    rejects, such as ``"Not A Name"``, which exists to be 400ed and so claims nothing; and a
    dict that spells its ``Authorization`` header out inline instead of splatting a machine
    constant, which leaves no machine to key the claim on. A module that does not parse raises
    here rather than being skipped — it could not have been collected either.
    """
    tree = ast.parse(source)
    strings, dicts = _module_constants(tree, stem)
    claims: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "X-Agent-Name"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                name = value.value
            elif isinstance(value, ast.Name):
                name = strings.get(value.id, "")
            else:
                continue
            if valid_name(name):
                claims |= {(machine, name) for machine in _machines(node, dicts, frozenset())}
    return claims


def _claims_across_the_suite() -> dict[tuple[str, str], list[str]]:
    """Every ``(machine, name)`` any test module asks for, and which files ask for it."""
    claimed: dict[tuple[str, str], list[str]] = {}
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        for pair in sorted(requested_names(path.read_text(encoding="utf-8"), path.stem)):
            claimed.setdefault(pair, []).append(path.name)
    return claimed


def test_this_module_derives_a_name_the_board_will_accept():
    """The derivation is only worth anything if the board honours what it produces.

    A requested name that fails validation is a 400 on every request this module makes, so
    the constraint belongs next to the derivation rather than in whoever renames the file
    next. Asserted on the derived value, not on a copy of it — a literal here would be the
    same mistake one level up.
    """
    assert valid_name(AGENT_NAME) and "_" not in AGENT_NAME


def test_no_two_test_modules_request_the_same_designated_name():
    """No two files may ask for one name, which is the collision LAPTOP_A had.

    A designated name is claimed once per machine: the first module to ask gets it, and every
    later asker is quietly handed something else. So a file that asserts on the name it asked
    for passes or fails on collection order, and passes for as long as it happens to be
    collected first. That is why the original clash survived months of green runs — nothing
    was wrong with either file in isolation, and nothing in either file could have noticed.
    Only a view across the whole suite can, which is what this is.

    A file using several names of its own is fine; test_designated_names.py legitimately
    asks for both ``deploy`` and ``shadowme``, and they do not compete with each other.
    A derived name counts as an asker: this module's own is in there, so a file hard-coding
    ``test-agent-identity`` against LAPTOP is the original bug again and fails here.
    """
    claimed = _claims_across_the_suite()
    clashes = {where: files for where, files in claimed.items() if len(files) > 1}
    assert not clashes, (
        "two modules request the same designated name; whichever is collected second is "
        f"handed a different one: {clashes}"
    )


def test_a_request_is_seen_however_the_dict_is_written():
    """Every shape the text scan that came before missed, all meaning one claim.

    That scan wanted the splat first, double quotes, no padding and no brace in between;
    a dict written any other way was silently unscanned, so a real clash spelled the wrong
    way passed green — the same shape of hole as the bug the check exists to catch. These
    are all one dict to Python, so they are all one claim here.
    """
    shapes = {
        "reversed": 'H = {"X-Agent-Name": "deploy", **LAPTOP}',
        "single-quoted": "H = {'X-Agent-Name': 'deploy', **LAPTOP}",
        "padded": 'H = { **LAPTOP , "X-Agent-Name" : "deploy" }',
        "brace in between": 'H = {**LAPTOP, "X-Agent-Key": f"{k}-1", "X-Agent-Name": "deploy"}',
        "nested in a call": 'def t():\n    go(h={**LAPTOP, "X-Agent-Name": "deploy"})',
        "name via a constant": 'N = "deploy"\nH = {**LAPTOP, "X-Agent-Name": N}',
        "splat of a local header": (
            'BASE = {**LAPTOP, "X-Agent-Key": "k"}\nH = {**BASE, "X-Agent-Name": "deploy"}'
        ),
    }
    seen = {label: requested_names(src, "test_x") for label, src in shapes.items()}
    assert seen == {label: {("LAPTOP", "deploy")} for label in shapes}

    # Those shapes are quoted source, not requests: a text scan would have booked seven
    # claims on this file's behalf. Parsing only sees dicts the module actually builds.
    assert _claims_across_the_suite()[("LAPTOP", "deploy")] == ["test_designated_names.py"]


def test_a_derived_name_claims_as_loudly_as_a_literal():
    """The module that motivated the check has to be inside it.

    LAPTOP_A's name is derived from the filename rather than written out, and a scan that
    only understood literals could not see it — leaving the one module with a history of
    clashing as the one module that could not clash. A literal elsewhere spelling the same
    string is the original bug exactly, one side derived.
    """
    derived = (
        'AGENT_NAME = Path(__file__).stem.replace("_", "-")\n'
        'H = {**LAPTOP, "X-Agent-Name": AGENT_NAME}'
    )
    assert requested_names(derived, "test_thing") == {("LAPTOP", "test-thing")}
    literal = 'H = {**LAPTOP, "X-Agent-Name": "test-thing"}'
    assert requested_names(literal, "test_other") == requested_names(derived, "test_thing")

    assert _claims_across_the_suite()[("LAPTOP", AGENT_NAME)] == [Path(__file__).name]


def test_names_the_board_chose_and_names_it_would_reject_claim_nothing():
    """The documented exclusions, asserted rather than promised."""
    out_of_scope = (
        'H = {**SERVER, "X-Agent-Name": me["name"]}',        # the board's name, not ours
        'H = {**SERVER, "X-Agent-Name": "Not A Name"}',      # exists to be 400ed
        'H = {"Authorization": "Bearer x", "X-Agent-Name": "deploy"}',  # no machine to key on
    )
    assert [requested_names(src, "test_x") for src in out_of_scope] == [set(), set(), set()]


# ---- the caller's key becomes an identity -----------------------------------

async def test_whoami_reflects_the_resolved_identity(client):
    r = (await client.get("/whoami", headers=SERVER_A)).json()
    assert r["machine"] == "server"
    assert r["name"] and r["agent"] == f"server/{r['name']}"

    # No key at all: the pre-2.9 collapse to the bare machine name, which is also
    # the broadcast address. Still accepted, still visibly undifferentiated.
    bare = (await client.get("/whoami", headers=SERVER)).json()
    assert bare["agent"] == "server" and bare["name"] is None


async def test_co_tenant_agents_author_distinctly(client):
    a = (await client.post("/post", json={"summary": "from A"}, headers=SERVER_A)).json()["id"]
    b = (await client.post("/post", json={"summary": "from B"}, headers=SERVER_B)).json()["id"]

    from_a = (await client.get(f"/post/{a}", headers=SERVER)).json()["from"]
    from_b = (await client.get(f"/post/{b}", headers=SERVER)).json()["from"]
    assert from_a == await ident(client, SERVER_A)
    assert from_b == await ident(client, SERVER_B)
    assert from_a != from_b and machine_of(from_a) == machine_of(from_b) == "server"


async def test_bad_key_header_is_rejected_not_ignored(client):
    bad = {**SERVER, "X-Agent-Key": "a/b"}
    r = await client.post("/post", json={"summary": "x"}, headers=bad)
    assert r.status_code == 400
    assert "X-Agent-Key" in r.json()["detail"]


async def test_key_cannot_forge_another_machine(client):
    # The agent half is scoped under the authenticated machine, never replacing it.
    r = (await client.get("/whoami", headers={**SERVER, "X-Agent-Key": "laptop"})).json()
    assert r["machine"] == "server" and r["agent"].startswith("server/")


# ---- directed posts reach the right inbox -----------------------------------

async def test_directed_post_reaches_the_named_agent_and_its_machine(client):
    agent_a, agent_b = await ident(client, SERVER_A), await ident(client, SERVER_B)
    to_one = (await client.post(
        "/post", json={"summary": "for A only", "to": agent_a}, headers=LAPTOP_A
    )).json()["id"]
    to_box = (await client.post(
        "/post", json={"summary": "for all server", "to": "server"}, headers=LAPTOP_A
    )).json()["id"]

    def ids(posts):
        return {p["id"] for p in posts}

    inbox_a = ids((await client.get("/board", params={"to": agent_a}, headers=SERVER)).json())
    inbox_b = ids((await client.get("/board", params={"to": agent_b}, headers=SERVER)).json())
    inbox_box = ids((await client.get("/board", params={"to": "server"}, headers=SERVER)).json())

    assert {to_one, to_box} <= inbox_a          # named agent sees both
    assert to_box in inbox_b and to_one not in inbox_b   # co-tenant sees only the broadcast
    assert {to_one, to_box} <= inbox_box        # the machine sees its agents' mail


# ---- leases: identity gets finer, authorisation does not --------------------

async def test_lease_holder_carries_the_agent_name(client):
    body = {"session": "v29-lease", "device": "server", "repo": REPO, "title": "identity work"}
    claim = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert claim["holder"] == await ident(client, SERVER_A)

    # /active hands a peer the exact address to reply to.
    agents = (await client.get("/active", params={"repo": REPO}, headers=LAPTOP)).json()["agents"]
    assert [a["holder"] for a in agents if a["session"] == "v29-lease"] == [claim["holder"]]


async def test_holder_filter_matches_every_agent_on_a_machine(client):
    for headers, sess in ((SERVER_A, "v29-h-a"), (SERVER_B, "v29-h-b")):
        await client.post(
            "/lease", json={"session": sess, "device": "server", "repo": REPO}, headers=headers
        )
    found = (await client.get(
        "/active", params={"repo": REPO, "holder": "server"}, headers=LAPTOP
    )).json()["agents"]
    assert {"v29-h-a", "v29-h-b"} <= {a["session"] for a in found}


async def test_a_co_tenant_may_renew_and_a_stranger_may_not(client):
    body = {"session": "v29-own", "device": "server"}
    lease_id = (await client.post("/lease", json=body, headers=SERVER_A)).json()["lease_id"]

    # Same machine, different agent: allowed — they share the token, so a
    # boundary here would buy nothing and would break lease upgrades.
    ok = await client.post("/lease/renew", json={"lease_id": lease_id}, headers=SERVER_B)
    assert ok.status_code == 200

    denied = await client.post("/lease/renew", json={"lease_id": lease_id}, headers=LAPTOP_A)
    assert denied.status_code == 403


async def test_reclaim_upgrades_a_pre_identity_holder(client):
    """A lease claimed before the agent had a name adopts one on renew — that's
    the migration path for sessions already live when the identity split lands."""
    body = {"session": "v29-upgrade", "device": "server"}
    assert (await client.post("/lease", json=body, headers=SERVER)).json()["holder"] == "server"
    renewed = (await client.post("/lease", json=body, headers=SERVER_A)).json()
    assert renewed["renewed"] is True
    assert renewed["holder"] == await ident(client, SERVER_A)


async def test_another_device_still_conflicts(client):
    body = {"session": "v29-conflict", "device": "server"}
    await client.post("/lease", json=body, headers=SERVER_A)
    clash = await client.post(
        "/lease", json={"session": "v29-conflict", "device": "laptop"}, headers=LAPTOP_A
    )
    assert clash.status_code == 409
    assert clash.json()["detail"]["held_by"] == await ident(client, SERVER_A)
