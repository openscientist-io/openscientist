"""Air-gapped / zero-egress mode for OpenScientist.

Everything air-gap-specific lives here. The module *reads* provider/agent
metadata to enforce policy rather than *modifying* their contracts — so PR-1
touches the existing agent/provider code as little as possible. See
:doc:`docs/AIR_GAPPED_MODE_RFC.md`.
"""
