"""raw objects and source records

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per stored external response. The bytes live in the raw store (gzip);
    # `request_key` identifies the logical request (secrets excluded) so re-runs can reuse it.
    op.execute(
        """
        CREATE TABLE raw_objects (
            id            bigserial PRIMARY KEY,
            source        text NOT NULL,
            request_key   text NOT NULL,
            request       jsonb NOT NULL,
            storage_key   text NOT NULL UNIQUE,
            sha256        text NOT NULL,
            bytes         integer NOT NULL,
            content_type  text,
            http_status   integer NOT NULL,
            etag          text,
            last_modified text,
            fetched_at    timestamptz NOT NULL DEFAULT now(),
            run_id        bigint REFERENCES runs (id)
        )
        """
    )
    op.execute("CREATE INDEX raw_objects_request_idx ON raw_objects (request_key, fetched_at DESC)")

    # One row per record per source, upserted on (source, source_id). Never hard-deleted:
    # records that stop appearing get `missed_runs` incremented instead.
    op.execute(
        """
        CREATE TABLE source_records (
            id                  bigserial PRIMARY KEY,
            source              text NOT NULL,
            source_id           text NOT NULL,
            region              text NOT NULL,
            name                text,
            payload             jsonb NOT NULL,
            geom                geometry(Point, 4326),
            shape               geometry(Geometry, 4326),
            content_hash        text NOT NULL,
            raw_object_id       bigint REFERENCES raw_objects (id),
            first_seen_run_id   bigint NOT NULL REFERENCES runs (id),
            last_seen_run_id    bigint NOT NULL REFERENCES runs (id),
            last_changed_run_id bigint NOT NULL REFERENCES runs (id),
            first_seen_at       timestamptz NOT NULL DEFAULT now(),
            last_seen_at        timestamptz NOT NULL DEFAULT now(),
            missed_runs         integer NOT NULL DEFAULT 0,
            UNIQUE (source, source_id)
        )
        """
    )
    op.execute("CREATE INDEX source_records_geom_idx ON source_records USING gist (geom)")
    op.execute("CREATE INDEX source_records_shape_idx ON source_records USING gist (shape)")
    op.execute(
        "CREATE INDEX source_records_name_trgm_idx ON source_records USING gin (name gin_trgm_ops)"
    )
    op.execute("CREATE INDEX source_records_region_idx ON source_records (region, source)")


def downgrade() -> None:
    op.execute("DROP TABLE source_records")
    op.execute("DROP TABLE raw_objects")
