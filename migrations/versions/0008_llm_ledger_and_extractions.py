"""LLM cost ledger and extractions

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every LLM call, including replays (replayed = true, cost 0) and token counting.
    op.execute(
        """
        CREATE TABLE llm_calls (
            id                 bigserial PRIMARY KEY,
            run_id             bigint REFERENCES runs (id),
            document_id        bigint REFERENCES documents (id),
            purpose            text NOT NULL,   -- extract | eval | ablation
            provider           text NOT NULL,
            model              text NOT NULL,
            prompt_version     text NOT NULL,
            request_hash       text NOT NULL,
            ladder_step        text,            -- cheap | strong
            is_batch           boolean NOT NULL DEFAULT false,
            input_tokens       integer NOT NULL DEFAULT 0,
            output_tokens      integer NOT NULL DEFAULT 0,
            cache_read_tokens  integer NOT NULL DEFAULT 0,
            cache_write_tokens integer NOT NULL DEFAULT 0,
            thinking_tokens    integer NOT NULL DEFAULT 0,
            cost_usd           numeric(12, 8) NOT NULL DEFAULT 0,   -- billed
            est_haiku_usd      numeric(12, 8) NOT NULL DEFAULT 0,   -- same tokens on Haiku 5.5
            latency_ms         integer,
            stop_reason        text,
            outcome            text NOT NULL,
            replayed           boolean NOT NULL DEFAULT false,
            created_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX llm_calls_day_idx ON llm_calls (provider, model, created_at)")
    op.execute(
        """
        CREATE TABLE extractions (
            id                 bigserial PRIMARY KEY,
            document_id        bigint NOT NULL REFERENCES documents (id),
            prompt_version     text NOT NULL,
            content_hash       text NOT NULL,
            provider           text NOT NULL,
            model              text,            -- model whose answer was kept
            ladder_step        text NOT NULL,   -- cheap | strong | review | failed
            status             text NOT NULL,   -- ok | needs_review | failed
            page_kind          text,
            output             jsonb,           -- grounded campgrounds
            grounding          jsonb NOT NULL DEFAULT '{}'::jsonb,
            hallucination_rate double precision,
            run_id             bigint REFERENCES runs (id),
            created_at         timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_id, prompt_version, provider)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE extractions")
    op.execute("DROP TABLE llm_calls")
