"""crawl: sites (seeds and their outcome) and pages (every fetch attempt)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sites (
            id                 bigserial PRIMARY KEY,
            region             text NOT NULL,
            seed_url           text NOT NULL UNIQUE,   -- canonical, or the raw value if invalid
            registrable_domain text,
            scope              jsonb NOT NULL DEFAULT '{}'::jsonb,
            record_refs        text[] NOT NULL DEFAULT '{}',
            status             text NOT NULL DEFAULT 'pending',
            skip_reason        text,
            booking_platform   text,
            pages_fetched      integer NOT NULL DEFAULT 0,
            last_run_id        bigint REFERENCES runs (id),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE pages (
            id             bigserial PRIMARY KEY,
            site_id        bigint NOT NULL REFERENCES sites (id),
            url            text NOT NULL,          -- canonical request URL
            final_url      text,
            depth          integer NOT NULL,
            relevance      double precision,
            http_status    integer,
            content_type   text,
            content_hash   text,
            bytes          integer,
            render_mode    text NOT NULL DEFAULT 'static'
                           CHECK (render_mode IN ('static', 'playwright')),
            raw_object_id  bigint REFERENCES raw_objects (id),
            etag           text,
            last_modified  text,
            outcome        text NOT NULL,  -- fetched | unchanged | robots_blocked | error | ...
            flags          text[] NOT NULL DEFAULT '{}',
            text_chars     integer,
            title          text,
            error          text,
            run_id         bigint NOT NULL REFERENCES runs (id),
            fetched_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (site_id, url)
        )
        """
    )
    op.execute("CREATE INDEX pages_outcome_idx ON pages (outcome)")


def downgrade() -> None:
    op.execute("DROP TABLE pages")
    op.execute("DROP TABLE sites")
