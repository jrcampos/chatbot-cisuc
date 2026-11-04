"""
External Sources

Batch extraction plugins for funding-opportunity data hosted outside CISUC's
own systems (EU institutions, national funding bodies). This is NOT a live
web-fetch layer — every plugin here is meant to run on a scheduled batch job,
the same way `preprocessing/` batches CISUC's own site. The only part used on
the live request path is the read API `store/queries.py` (plus the context
packing it renders with), called by the orchestrator's funding-calls source
when CALLS_SOURCE=on (query_understanding/calls.py).
"""
