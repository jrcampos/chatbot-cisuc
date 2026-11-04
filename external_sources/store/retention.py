"""
Retention policy (config/store_policy.yaml `retention`). Runs at the end of every run and via
`python -m external_sources.store prune`. Never deletes a published or previous snapshot, an
open dead letter, or a document version that a retained snapshot links to.
"""

from __future__ import annotations

from typing import Any


def prune(conn: Any, retention: dict[str, Any]) -> dict[str, int]:
    keep = int(retention.get("keep_published_snapshots", 14))
    counts: dict[str, int] = {}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """WITH ranked AS (
                   SELECT snapshot_id, row_number() OVER (PARTITION BY source ORDER BY snapshot_id DESC) AS n
                   FROM extsrc.snapshot WHERE status IN ('published', 'superseded'))
               DELETE FROM extsrc.snapshot s USING ranked r
               WHERE s.snapshot_id = r.snapshot_id AND r.n > %s
                 AND s.snapshot_id NOT IN (SELECT published_snapshot_id FROM extsrc.source_state
                                           WHERE published_snapshot_id IS NOT NULL
                                           UNION SELECT previous_snapshot_id FROM extsrc.source_state
                                           WHERE previous_snapshot_id IS NOT NULL)""", (keep,))
        counts["snapshots_superseded"] = cur.rowcount
        cur.execute(
            """DELETE FROM extsrc.snapshot WHERE status IN ('rejected', 'staging')
                   AND created_at < now() - make_interval(days => %s)""",
            (int(retention.get("rejected_snapshot_days", 14)),))
        counts["snapshots_rejected_or_stale_staging"] = cur.rowcount
        history = int(retention.get("history_days", 400))
        cur.execute("DELETE FROM extsrc.record_change WHERE at < now() - make_interval(days => %s)", (history,))
        counts["record_changes"] = cur.rowcount
        cur.execute(
            """DELETE FROM extsrc.run WHERE started_at < now() - make_interval(days => %s)
                   AND run_id NOT IN (SELECT run_id FROM extsrc.snapshot)""", (history,))
        counts["runs"] = cur.rowcount
        cur.execute(
            """DELETE FROM extsrc.dead_letter WHERE resolved_at IS NOT NULL
                   AND resolved_at < now() - make_interval(days => %s)""",
            (int(retention.get("dead_letter_resolved_days", 90)),))
        counts["dead_letters"] = cur.rowcount
        orphan_days = int(retention.get("orphan_document_days", 30))
        cur.execute(
            """DELETE FROM extsrc.document_version v
               WHERE v.stored_at < now() - make_interval(days => %s)
                 AND NOT EXISTS (SELECT 1 FROM extsrc.record_document rd
                                 WHERE rd.doc_id = v.doc_id AND rd.content_sha256 = v.content_sha256)
                 AND NOT EXISTS (SELECT 1 FROM extsrc.document d
                                 WHERE d.doc_id = v.doc_id AND d.latest_sha256 = v.content_sha256)""",
            (orphan_days,))
        counts["document_versions"] = cur.rowcount
        cur.execute(
            """DELETE FROM extsrc.document d WHERE d.last_seen < now() - make_interval(days => %s)
                 AND NOT EXISTS (SELECT 1 FROM extsrc.record_document rd WHERE rd.doc_id = d.doc_id)""",
            (orphan_days,))
        counts["documents"] = cur.rowcount
        cur.execute(
            """DELETE FROM extsrc.payload p WHERE p.first_seen < now() - interval '1 day'
                 AND NOT EXISTS (SELECT 1 FROM extsrc.external_record r WHERE r.raw_sha256 = p.sha256)""")
        counts["payloads"] = cur.rowcount
    return counts
