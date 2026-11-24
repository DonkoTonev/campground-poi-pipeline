"""canonical poi records and quality flags

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One canonical record per source record (later also per LLM-extracted campground).
    # Normalized values live in columns; the source's raw values are kept in `raw`.
    op.execute(
        """
        CREATE TABLE poi_records (
            id                      bigserial PRIMARY KEY,
            source_record_id        bigint NOT NULL UNIQUE REFERENCES source_records (id),
            source                  text NOT NULL,
            source_id               text NOT NULL,
            region                  text NOT NULL,
            name                    text,
            match_name              text,
            alt_names               text[] NOT NULL DEFAULT '{}',
            category                text NOT NULL,
            is_campground           boolean NOT NULL,
            exclusion_reason        text,
            operator_type           text NOT NULL,
            operator_name           text,
            phone                   text,
            phone_ext               text,
            website                 text,
            reservation_url         text,
            email                   text,
            address                 jsonb NOT NULL DEFAULT '{}'::jsonb,
            state_code              text,
            geom                    geometry(Point, 4326),
            shape                   geometry(Geometry, 4326),
            precision_m             double precision,
            ids                     jsonb NOT NULL DEFAULT '{}'::jsonb,
            amenities               jsonb NOT NULL DEFAULT '{}'::jsonb,
            num_sites               integer,
            sites_by_type           jsonb NOT NULL DEFAULT '{}'::jsonb,
            max_rv_length_ft        integer,
            reservable              boolean,
            first_come_first_served boolean,
            price_min               numeric(8, 2),
            price_max               numeric(8, 2),
            currency                text,
            season                  jsonb NOT NULL DEFAULT '[]'::jsonb,
            months_open             integer[],
            season_note             text,
            raw                     jsonb NOT NULL DEFAULT '{}'::jsonb,
            run_id                  bigint NOT NULL REFERENCES runs (id),
            normalized_at           timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX poi_records_geom_idx ON poi_records USING gist (geom)")
    op.execute("CREATE INDEX poi_records_shape_idx ON poi_records USING gist (shape)")
    op.execute(
        "CREATE INDEX poi_records_match_name_trgm_idx ON poi_records "
        "USING gin (match_name gin_trgm_ops)"
    )
    op.execute("CREATE INDEX poi_records_ids_idx ON poi_records USING gin (ids)")
    op.execute("CREATE INDEX poi_records_region_idx ON poi_records (region, source)")

    # Every rule violation, from any stage, about any subject. Never silently fixed.
    op.execute(
        """
        CREATE TABLE quality_flags (
            id           bigserial PRIMARY KEY,
            run_id       bigint NOT NULL REFERENCES runs (id),
            stage        text NOT NULL,
            subject_type text NOT NULL,
            subject_id   bigint NOT NULL,
            field        text NOT NULL,
            rule         text NOT NULL,
            severity     text NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
            details      jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX quality_flags_subject_idx ON quality_flags (subject_type, subject_id)")
    op.execute("CREATE INDEX quality_flags_rule_idx ON quality_flags (stage, rule)")


def downgrade() -> None:
    op.execute("DROP TABLE quality_flags")
    op.execute("DROP TABLE poi_records")
