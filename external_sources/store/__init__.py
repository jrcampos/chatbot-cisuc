"""
External-sources store: Postgres (+ pgvector) persistence for the funding-call records, with
provenance, value ranges, snapshots (atomic full replace per source), change history, dead
letters and a read API for the chatbot (`queries.py`). See external_sources/README.md.
"""
