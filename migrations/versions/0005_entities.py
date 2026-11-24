"""golden records: entities, members, fields with provenance, redirects, change log

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE entities (
            id                uuid PRIMARY KEY,
            region            text NOT NULL,
            status            text NOT NULL CHECK (status IN ('active', 'merged', 'retired')),
            parent_id         uuid REFERENCES entities (id),
            name              text,
            alt_names         text[] NOT NULL DEFAULT '{}',
            category          text,
            operator_type     text,
            state_code        text,
            geom              geometry(Point, 4326),
            shape             geometry(Geometry, 4326),
            months_open       integer[],
            golden            jsonb NOT NULL DEFAULT '{}'::jsonb,
            sources           text[] NOT NULL DEFAULT '{}',
            quality_score     double precision,
            quality           jsonb NOT NULL DEFAULT '{}'::jsonb,
            possibly_closed   boolean NOT NULL DEFAULT false,
            last_seen_at      timestamptz,
            content_hash      text,
            created_run_id    bigint NOT NULL REFERENCES runs (id),
            updated_run_id    bigint NOT NULL REFERENCES runs (id)
        )
        """
    )
    op.execute("CREATE INDEX entities_geom_idx ON entities USING gist (geom)")
    op.execute("CREATE INDEX entities_region_status_idx ON entities (region, status)")
    op.execute(
        """
        CREATE TABLE entity_members (
            entity_id     uuid NOT NULL REFERENCES entities (id),
            poi_record_id bigint NOT NULL UNIQUE REFERENCES poi_records (id),
            record_ref    text NOT NULL,  -- "source:source_id", stable across rebuilds
            PRIMARY KEY (entity_id, poi_record_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE entity_fields (
            entity_id    uuid NOT NULL REFERENCES entities (id),
            field        text NOT NULL,
            value        jsonb,
            source       text,
            record_ref   text,
            reason       text NOT NULL,
            alternatives jsonb NOT NULL DEFAULT '[]'::jsonb,
            PRIMARY KEY (entity_id, field)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE entity_redirects (
            old_id     uuid PRIMARY KEY REFERENCES entities (id),
            new_id     uuid NOT NULL REFERENCES entities (id),
            reason     text NOT NULL,
            run_id     bigint NOT NULL REFERENCES runs (id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE entity_changes (
            run_id     bigint NOT NULL REFERENCES runs (id),
            entity_id  uuid NOT NULL REFERENCES entities (id),
            change     text NOT NULL CHECK (change IN ('created', 'updated', 'merged', 'retired')),
            details    jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (run_id, entity_id, change)
        )
        """
    )

    # Entity-level flags (field conflicts, possibly closed) need a uuid subject.
    op.execute("ALTER TABLE quality_flags ALTER COLUMN subject_id DROP NOT NULL")
    op.execute("ALTER TABLE quality_flags ADD COLUMN entity_id uuid REFERENCES entities (id)")
    op.execute(
        "ALTER TABLE quality_flags ADD CONSTRAINT quality_flags_one_subject "
        "CHECK ((subject_id IS NULL) <> (entity_id IS NULL))"
    )
    op.execute("CREATE INDEX quality_flags_entity_idx ON quality_flags (entity_id)")


def downgrade() -> None:
    op.execute("ALTER TABLE quality_flags DROP CONSTRAINT quality_flags_one_subject")
    op.execute("ALTER TABLE quality_flags DROP COLUMN entity_id")
    op.execute("ALTER TABLE quality_flags ALTER COLUMN subject_id SET NOT NULL")
    for table in ("entity_changes", "entity_redirects", "entity_fields", "entity_members",
                  "entities"):  # fmt: skip
        op.execute(f"DROP TABLE {table}")
