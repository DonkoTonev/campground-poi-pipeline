"""entity resolution: candidate pairs, match decisions, clusters, relations

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Geography index on the polygon-aware geometry, so ST_DWithin(..., metres) blocking can
    # use an index instead of comparing every pair.
    op.execute(
        "CREATE INDEX poi_records_geog_idx ON poi_records "
        "USING gist ((coalesce(shape, geom)::geography))"
    )
    op.execute(
        """
        CREATE TABLE candidate_pairs (
            run_id        bigint NOT NULL REFERENCES runs (id),
            region        text NOT NULL,
            a_id          bigint NOT NULL REFERENCES poi_records (id),
            b_id          bigint NOT NULL REFERENCES poi_records (id),
            distance_m    double precision,
            blocked_by    text[] NOT NULL,
            a_contains_b  boolean NOT NULL DEFAULT false,
            b_contains_a  boolean NOT NULL DEFAULT false,
            PRIMARY KEY (run_id, a_id, b_id),
            CHECK (a_id < b_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE match_decisions (
            run_id        bigint NOT NULL REFERENCES runs (id),
            region        text NOT NULL,
            a_id          bigint NOT NULL REFERENCES poi_records (id),
            b_id          bigint NOT NULL REFERENCES poi_records (id),
            score         double precision NOT NULL,
            contributions jsonb NOT NULL,
            evidence      jsonb NOT NULL,
            decision      text NOT NULL CHECK (decision IN ('match', 'review', 'non_match')),
            reason        text NOT NULL,
            PRIMARY KEY (run_id, a_id, b_id)
        )
        """
    )
    op.execute("CREATE INDEX match_decisions_decision_idx ON match_decisions (region, decision)")
    op.execute(
        """
        CREATE TABLE cluster_members (
            run_id        bigint NOT NULL REFERENCES runs (id),
            region        text NOT NULL,
            cluster_key   bigint NOT NULL,
            poi_record_id bigint NOT NULL REFERENCES poi_records (id),
            PRIMARY KEY (run_id, poi_record_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE record_relations (
            run_id    bigint NOT NULL REFERENCES runs (id),
            region    text NOT NULL,
            child_id  bigint NOT NULL REFERENCES poi_records (id),
            parent_id bigint NOT NULL REFERENCES poi_records (id),
            relation  text NOT NULL,
            PRIMARY KEY (run_id, child_id)
        )
        """
    )


def downgrade() -> None:
    for table in ("record_relations", "cluster_members", "match_decisions", "candidate_pairs"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP INDEX poi_records_geog_idx")
