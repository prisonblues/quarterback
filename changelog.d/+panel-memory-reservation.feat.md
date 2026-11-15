# a panel round waits for memory instead of being killed partway through

On 2026-11-15 two panel rounds were killed for memory before they produced anything, while CI
runs held the box. Where `heavy-slot` (#874) is on PATH, a round now reserves 6G
(`QB_PANEL_MEMORY`) after its refusals and before it reads the PR, and waits for room.
