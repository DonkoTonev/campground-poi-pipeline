"""reduce: per-page representations and LLM input documents

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Texts live in the raw store (never committed); the table keeps sizes and hashes.
    op.execute(
        """
        CREATE TABLE page_texts (
            page_id         bigint NOT NULL REFERENCES pages (id),
            representation  text NOT NULL
                            CHECK (representation IN ('raw_html', 'cleaned_html', 'markdown',
                                                      'text')),
            reducer_version text NOT NULL,
            chars           integer NOT NULL,
            est_tokens      integer NOT NULL,
            text_hash       text NOT NULL,
            storage_key     text NOT NULL,
            PRIMARY KEY (page_id, representation, reducer_version)
        )
        """
    )
    # One LLM input per subject: a crawled site's bundle, or a RIDB facility description.
    op.execute(
        """
        CREATE TABLE documents (
            id               bigserial PRIMARY KEY,
            kind             text NOT NULL CHECK (kind IN ('web_bundle', 'ridb_description')),
            site_id          bigint REFERENCES sites (id),
            source_record_id bigint REFERENCES source_records (id),
            record_refs      text[] NOT NULL DEFAULT '{}',
            reducer_version  text NOT NULL,
            content_hash     text NOT NULL,
            chars            integer NOT NULL,
            est_tokens       integer NOT NULL,
            storage_key      text NOT NULL,
            page_refs        jsonb NOT NULL DEFAULT '{}'::jsonb,
            deterministic    jsonb NOT NULL DEFAULT '[]'::jsonb,
            stats            jsonb NOT NULL DEFAULT '{}'::jsonb,
            run_id           bigint NOT NULL REFERENCES runs (id),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CHECK ((site_id IS NULL) <> (source_record_id IS NULL))
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX documents_site_uq ON documents (kind, site_id, reducer_version) "
        "WHERE site_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX documents_record_uq ON documents "
        "(kind, source_record_id, reducer_version) WHERE source_record_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE documents")
    op.execute("DROP TABLE page_texts")
