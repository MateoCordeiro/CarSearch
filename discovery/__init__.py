"""
discovery — autonomous dealer discovery for any US ZIP + radius.

See docs/PLAN-discovery.md for the full design. run_discovery() and the
`python -m discovery` CLI land here once the orchestrator (plan step 6) is
built; for now this package exposes the source-agnostic building blocks
(Candidate schema, normalizers, cross-source merge).
"""
