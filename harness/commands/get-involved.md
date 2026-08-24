# Get Involved — take the next item off the plan

@description Take the highest free item off the board's plan for this scope, claim it before starting, and work it with the right skill. No issue number: the plan already has the order.
@arguments $ARGS: [scope] — `owner/name` or `project:<name>`. Default: this checkout's repo.

Somebody worked out an order with another agent and put it on the board. This picks the top of it
up. **You are not being asked to decide what is worth doing** — a human did that when they ordered
the plan, and `GET /plan` already computes which item is first, free, unblocked and outside anybody
else's hold. Everything here is: read that answer, say how much it is worth, take it before you
touch anything, run the right existing skill, and put it back when you stop.

Every other command in this directory takes an issue number a person looked up. This is the one
that does not, and that difference is the whole feature: tell three agents "get involved" and they
take three different items, because the claim is atomic and the loser is told who won.

## What this is not

- **It is not a triager.** It never judges whether something is worth doing, never opens an issue,
  and never adds to the plan. Watching issues and deciding which are actionable is #63 — a much
  larger and riskier thing, kept separate on purpose.
- **It never reorders.** `POST /plan/reorder` is human-only. If the top item looks wrong to you,
  say so to the human and take it or stop — an agent that reorders the plan to suit itself has
  approved its own work, which is the shape #85, #86, #78 and #335 each settled.
- **It starts nothing.** You are already running. Whether an agent exists at all is #277/#371/#372
  and is off by default.
- **It is not silent about a bad order.** See step 2 — this is the one you are most likely to skip.

## 1. Read the plan and take the top free item — in one call

```bash
qb-next --json                      # this checkout's repo scope
qb-next --json --scope project:65lowther     # a scope with no repo behind it
```

`$ARGS`, if there is anything in it, is the scope: pass it as `--scope`. A bare invocation derives
the scope from this checkout's `origin` remote, which is right nearly always and wrong exactly when
the work has no repo. The exact spelling of a `project:` scope comes from the board — `plan_read`
returns every declared one under `scopes` — and is not a thing to guess at.

`qb-next` does the mechanical half and nothing else: it reads `GET /plan`, prints what the order is
worth, claims the item **before** you start (that is the only post that can prevent duplicated
work; a `done` afterwards can only record it), walks past anything a peer took between the read and
the claim, and hands you back JSON. Read `harness/bin/qb-next` if you want the argument for each of
those; do not re-implement any of it here.

Its exit code is the branch you take:

| exit | meaning | what you do |
|---|---|---|
| 0 | it took one | step 2 onwards |
| 1 | nothing free | step 6 — this is **not** an error |
| 2 | it could not tell | report exactly what it said and stop. Do not work around it |

## 2. Say what the order is worth, out loud, before you do anything

The JSON carries `order_trust` and `caveat`, and `qb-next` has already printed both on stderr.
**Repeat the substance of them to the user in your first message about this item.** Not as a
footnote at the end, and not "the plan says X is next" with the qualification dropped.

- `order_trust.trusted: true` — every open item sits where a person put it. Say so in a clause and
  move on.
- `order_trust.trusted: false`, or a non-null `caveat` — some of the sequence is just the order
  things were appended. Then rank 1 is *the oldest insertion*, not the most important thing, and
  reporting it as a priority launders one into the other. Say which: how many items are unchosen,
  from which rank, and whether the item you took is one of them (`rank_source: "appended"`).

Then take it anyway. Refusing to work an unchosen order would make the plan unusable, and the item
is still the best answer available — what it must not have is unqualified confidence. #183 exists
for this exact substitution.

Read `note` and `placed_for` in the JSON as well. `note` is the sentence a human would otherwise
have to repeat to every agent that asks, and it regularly says something the issue does not — "talk
to zeus/lantern-fennel first", "check whether this is already closed", "the obvious implementation
is the wrong one".

## 3. Dispatch on the ref kind

`qb-next` claims and reports; **you** run what it names. It prints the command in `dispatch` and
stops there, deliberately — a tool that ran a slash command would be a second dispatcher, and the
one thing it must own is the claim. A plan item names an issue, a PR, or nothing at all: there is
no fourth kind, because the board stores only those.

**Run the command in `dispatch` as it stands.** The two substitutions below are the only ones, and
each needs the human to have asked for it *in this invocation* — not your judgement about what the
item deserves. If you substitute, say so in the same breath as the item.

- **`ref.kind == "issue"`** → run `/fix-issue <number>`. That owns the worktree and its isolated
  database; do not create one yourself. Substitution: `/fix-and-land <number>` if the human said
  they want it landed rather than opened. The default is `/fix-issue`, because merging is a human
  step here.
- **`ref.kind == "pr"`** → run `/review-pr <number>`. Substitution: `/panel-review-pr <number>` if
  the human asked for a panel and the repo's rules enable one.
- **`ref` is null** → there is no forge behind this item. It is house work, admin, or anything in a
  `project:` scope. Work it **in place**: no worktree, no branch, no PR. The item's `title` and
  `note` are the brief, and the board is where you report — post a `finding` or a `done`, and if
  the item wants a decision from a person, post an `ask` addressed to them rather than deciding it.

Before you start, `qb-stage F0` so the fleet's status bar says which phase you are in.

## 4. Finish it: `plan_done`, and release on any exit

When the work closes — the issue closed, the PR merged, the house task finished:

```bash
qb-next --done <item_id> --note "PR #431"
```

This does not *decide* anything. The issue closing is what makes the work done; recording it stops
the next agent's plan read being one item out of date.

**And release on every other ending, including the ones you did not plan.** You stopped because the
tests will not pass, the human redirected you, the issue turned out to need a decision nobody has
made, you ran out of context:

```bash
qb-next --release <item_id>
```

Do it the moment you stop working on it.

**Be honest about what "on any exit" is.** It is a discipline, not a guarantee: nothing wraps your
dispatch in a `finally`, so a crash, a kill, or a context that runs out will not release anything.
What covers that case is the claim's TTL — it lapses on its own, which is exactly why the board
made claims expire — and the cost is that the next agent waits the whole TTL out for work nobody is
doing. That is the difference between releasing and being released, and it is why this step is
written as something you do rather than something that happens.

Say on the board what you learned as well — a `stuck` or a `finding` with `refs` to the item — so
the next agent to take it starts from where you got to rather than from the beginning.

## 5. One item, then stop

**This takes one item per invocation and does not loop.** That is a decision, not an omission.

`/fix-and-land` loops, but it loops over *review rounds within one issue* — bounded by a round cap
and a spend ceiling that already exist. A loop over *items* is a different thing: it is an agent
deciding how much work the fleet takes on, and nothing bounds that yet. #80 measures integration
cost as quadratic in open PRs, so an agent that quietly takes four items has not been four times as
useful. `qb-seat`'s brief stops after one item for the same reason and in those words.

So when the item closes, stop and say what you did. A human who wants another says so, and
`/get-involved` again is one line. When the appetite gates (#85/#86) and the board's round and
spend ceilings (#55) are wired to a client, this is where a `--drain` would go — and it should
arrive as a deliberate change with those ceilings in force, not as a default nobody chose.

## 6. When there is nothing free

Exit 1. Every item is claimed, blocked, covered by somebody's plan hold, or done. **This is a
normal state and it is common** — it is what a fleet that is working looks like.

Report it as the answer it is: how many are open, how many of each reason, and *who* holds what
(`qb-next` prints the holders, and a refusal names somebody to talk to). Then stop.

Do not: invent work, scan GitHub for something to do instead (that is #63), add an item to the plan
so there is something to take, or take an item somebody holds. If you want to help, post an `ask`
to a holder offering a hand and wait to be answered.

## 7. What to report

- the item, its rank, and **what the order was worth** (step 2)
- what you did with it, and how it ended
- whether you released or completed the claim
- **anything in `passed_over` or `closed_refs`.** These are things that happened to the plan on
  the way to your item and nobody else will mention them: a peer took the row above yours (say
  who, so the human knows two of you are working), or a row named an issue that had already been
  closed and was recorded done. The second is `qb-reconcile`'s business and it is worth a line —
  a plan that keeps growing stale rows is a plan somebody has stopped tending.
- anything the next agent taking this plan should know
