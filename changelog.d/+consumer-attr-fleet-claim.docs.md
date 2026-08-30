# the docs stop naming a nixosConfiguration that does not exist

Four places said, as a statement of fact about the fleet this was written for, that
`zeus` is `nixosConfigurations.desktop`: the `consumer.attr` option description in
`harness/hm-module.nix`, `resolve_attr`'s docstring in `harness/bin/qb-bump`, the
"system attribute is not the hostname" paragraph in `harness/README.md`, and the
docstring of the test that covers the scan.

There is no `desktop` output. The consuming flake's `nixosConfigurations` are
`atlas, atlas-vm, daedalus, hermes, hermes-vm, installer, sisyphus,
sisyphus-isolated, zeus, zeus-vm` — hostname and attribute agree on every host that
runs. Nothing in the code was wrong; `resolve_attr` has always checked the flake's
own attribute names before evaluating anything, and on this fleet that first check
hits and no configuration is ever evaluated.

But the prose was read as instructions, which is what four copies of a fact are for.
An agent asked to carry a harness onto zeus ran `qb-bump --host desktop`, which
short-circuits the resolution with an attribute nobody has, failed the build with
`flake ... does not provide attribute ... nixosConfigurations.desktop`, and announced
a `needs-human/environment` blocker against a machine that was fine. The remedy it
then proposed — set `consumer.attr` — would have changed nothing, because that option
exists for the fallback and the fallback never runs here.

So the four sites now say what is actually true and what it implies: the attribute
*need not* be the hostname, on this fleet it is, and `consumer.attr` is worth setting
only where the two genuinely differ.
