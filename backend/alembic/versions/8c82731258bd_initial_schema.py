"""initial schema

Revision ID: 8c82731258bd
Revises: 6ab133d553ca
Create Date: 2026-08-26 15:51:47.251803

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8c82731258bd'
down_revision: Union[str, Sequence[str], None] = '6ab133d553ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Native enum types, defined once and shared.
#
# `create_type=False` is load-bearing: two tables reference `user_role` and two
# reference `oauth_provider`, and with the default SQLAlchemy would emit
# CREATE TYPE once per referencing table, so the second CREATE TABLE would fail
# on an already-existing type. Creation and dropping are done explicitly below
# instead, which is also the only way the types get dropped on downgrade --
# autogenerate never emits DROP TYPE, so a downgrade/upgrade round trip would
# otherwise fail on the second upgrade.
ENUM_TYPES: dict[str, postgresql.ENUM] = {
    "outbox_job_type": postgresql.ENUM(
        'ANCHOR_ITEM', 'ANCHOR_ATTESTATION', 'ANCHOR_BATCH',
        name="outbox_job_type",
        create_type=False,
    ),
    "outbox_status": postgresql.ENUM(
        'QUEUED', 'IN_FLIGHT', 'DONE', 'DEAD',
        name="outbox_status",
        create_type=False,
    ),
    "oauth_provider": postgresql.ENUM(
        'GOOGLE',
        name="oauth_provider",
        create_type=False,
    ),
    "user_role": postgresql.ENUM(
        'CONSUMER', 'WEAVER', 'COOP_OFFICER', 'INSPECTOR', 'ADMIN',
        name="user_role",
        create_type=False,
    ),
    "user_status": postgresql.ENUM(
        'PENDING_VERIFICATION', 'ACTIVE', 'SUSPENDED',
        name="user_status",
        create_type=False,
    ),
    "auth_event_type": postgresql.ENUM(
        'REGISTER', 'LOGIN_SUCCESS', 'LOGIN_FAILURE', 'REFRESH', 'REFRESH_REUSE_DETECTED', 'LOGOUT', 'OAUTH_LINK', 'OAUTH_NEW_ACCOUNT', 'ROLE_GRANT', 'FRAUD_FLAG',
        name="auth_event_type",
        create_type=False,
    ),
    "chain_tx_status": postgresql.ENUM(
        'SENT', 'MINED', 'CONFIRMED', 'ORPHANED', 'FAILED',
        name="chain_tx_status",
        create_type=False,
    ),
    "pin_status": postgresql.ENUM(
        'PIN_PENDING', 'PINNED', 'PIN_FAILED',
        name="pin_status",
        create_type=False,
    ),
    "item_status": postgresql.ENUM(
        'PENDING', 'CONFIRMED', 'FAILED',
        name="item_status",
        create_type=False,
    ),
    "dispute_status": postgresql.ENUM(
        'NONE', 'DISPUTED',
        name="dispute_status",
        create_type=False,
    ),
    "item_event_type": postgresql.ENUM(
        'REGISTERED', 'SPLIT', 'ATTESTED', 'ANCHORED', 'DISPUTED', 'CLAIMED',
        name="item_event_type",
        create_type=False,
    ),
    "media_kind": postgresql.ENUM(
        'LOOM_PHOTO', 'WEAVE_MACRO', 'CERTIFICATE', 'VIDEO',
        name="media_kind",
        create_type=False,
    ),
    "suspicion_level": postgresql.ENUM(
        'NONE', 'WATCH', 'SUSPICIOUS',
        name="suspicion_level",
        create_type=False,
    ),
}

# Dropped in reverse creation order on downgrade.
ENUM_ORDER = list(ENUM_TYPES)


def upgrade() -> None:
    """Create every enum type, then every table."""
    bind = op.get_bind()

    # Required by the schema itself (citext for case-insensitive emails) and by
    # the project's baseline (pgcrypto). Local dev has these already; a fresh
    # Neon database does not, so the migration must not assume them.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    for enum_type in ENUM_TYPES.values():
        enum_type.create(bind, checkfirst=True)

    op.create_table('chain_nonce',
    sa.Column('address', sa.Text(), nullable=False),
    sa.Column('next_nonce', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('address', name=op.f('pk_chain_nonce'))
    )
    op.create_table('chain_outbox',
    sa.Column('job_type', ENUM_TYPES["outbox_job_type"], nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('dedupe_key', sa.Text(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.Text(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('status', ENUM_TYPES["outbox_status"], nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chain_outbox')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_chain_outbox_dedupe_key'))
    )
    op.create_index('ix_chain_outbox_locked_at', 'chain_outbox', ['locked_at'], unique=False)
    op.create_index('ix_chain_outbox_status_next_attempt_at', 'chain_outbox', ['status', 'next_attempt_at'], unique=False)
    op.create_table('indexer_checkpoints',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('last_block', sa.BigInteger(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('name', name=op.f('pk_indexer_checkpoints'))
    )
    op.create_table('pending_tokens',
    sa.Column('jti', sa.Uuid(), nullable=False),
    sa.Column('provider', ENUM_TYPES["oauth_provider"], nullable=False),
    sa.Column('provider_subject', sa.Text(), nullable=False),
    sa.Column('provider_email', postgresql.CITEXT(), nullable=True),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('jti', name=op.f('pk_pending_tokens'))
    )
    op.create_index('ix_pending_tokens_expires_at', 'pending_tokens', ['expires_at'], unique=False)
    op.create_table('quota_usage',
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used', sa.Numeric(precision=28, scale=4), nullable=False),
    sa.Column('budget', sa.Numeric(precision=28, scale=4), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quota_usage')),
    sa.UniqueConstraint('name', 'period_start', name='uq_quota_usage_name_period')
    )
    op.create_index('ix_quota_usage_name', 'quota_usage', ['name'], unique=False)
    op.create_table('rate_limit_buckets',
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('identifier', sa.Text(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('scope', 'identifier', 'window_start', name=op.f('pk_rate_limit_buckets'))
    )
    op.create_index('ix_rate_limit_buckets_expires_at', 'rate_limit_buckets', ['expires_at'], unique=False)
    op.create_table('users',
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('role', ENUM_TYPES["user_role"], nullable=False),
    sa.Column('status', ENUM_TYPES["user_status"], nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('region', sa.Text(), nullable=True),
    sa.Column('org_name', sa.Text(), nullable=True),
    sa.Column('identity_salt', sa.LargeBinary(), nullable=False),
    sa.Column('fraud_flagged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_index('ix_users_created_at_id', 'users', ['created_at', 'id'], unique=False)
    op.create_index('ix_users_role', 'users', ['role'], unique=False)
    op.create_index('ix_users_status', 'users', ['status'], unique=False)
    op.create_table('auth_events',
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', ENUM_TYPES["auth_event_type"], nullable=False),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('user_agent_hash', sa.String(length=64), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_auth_events_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_events'))
    )
    op.create_index('ix_auth_events_event_type_created_at', 'auth_events', ['event_type', 'created_at'], unique=False)
    op.create_index('ix_auth_events_user_id_created_at', 'auth_events', ['user_id', 'created_at'], unique=False)
    op.create_table('chain_txs',
    sa.Column('outbox_id', sa.Uuid(), nullable=False),
    sa.Column('tx_hash', sa.Text(), nullable=True),
    sa.Column('nonce', sa.Integer(), nullable=False),
    sa.Column('block_number', sa.BigInteger(), nullable=True),
    sa.Column('confirmations', sa.Integer(), nullable=False),
    sa.Column('status', ENUM_TYPES["chain_tx_status"], nullable=False),
    sa.Column('gas_used', sa.BigInteger(), nullable=True),
    sa.Column('raw_receipt', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['outbox_id'], ['chain_outbox.id'], name=op.f('fk_chain_txs_outbox_id_chain_outbox'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chain_txs')),
    sa.UniqueConstraint('tx_hash', name=op.f('uq_chain_txs_tx_hash'))
    )
    op.create_index('ix_chain_txs_block_number', 'chain_txs', ['block_number'], unique=False)
    op.create_index('ix_chain_txs_outbox_id', 'chain_txs', ['outbox_id'], unique=False)
    op.create_index('ix_chain_txs_status', 'chain_txs', ['status'], unique=False)
    op.create_table('dead_letters',
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('original_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('error_chain', sa.Text(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name=op.f('fk_dead_letters_resolved_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_dead_letters'))
    )
    op.create_index('ix_dead_letters_resolved_at', 'dead_letters', ['resolved_at'], unique=False)
    op.create_index('ix_dead_letters_resolved_by', 'dead_letters', ['resolved_by'], unique=False)
    op.create_index('ix_dead_letters_source_created_at', 'dead_letters', ['source', 'created_at'], unique=False)
    op.create_table('gi_categories',
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=False),
    sa.Column('is_textile', sa.Boolean(), nullable=False),
    sa.Column('attribute_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('schema_version', sa.Integer(), nullable=False),
    sa.Column('quantity_unit', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_gi_categories_created_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_gi_categories')),
    sa.UniqueConstraint('slug', 'schema_version', name='uq_gi_categories_slug_version'),
    sa.UniqueConstraint('slug', name=op.f('uq_gi_categories_slug'))
    )
    op.create_index('ix_gi_categories_created_by', 'gi_categories', ['created_by'], unique=False)
    op.create_index('ix_gi_categories_is_active', 'gi_categories', ['is_active'], unique=False)
    op.create_table('idempotency_keys',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('request_hash', sa.Text(), nullable=False),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_idempotency_keys_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_idempotency_keys')),
    sa.UniqueConstraint('user_id', 'key', name='uq_idempotency_keys_user_key')
    )
    op.create_index('ix_idempotency_keys_created_at', 'idempotency_keys', ['created_at'], unique=False)
    op.create_table('media',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('cid', sa.Text(), nullable=True),
    sa.Column('byte_size', sa.BigInteger(), nullable=False),
    sa.Column('content_type', sa.Text(), nullable=False),
    sa.Column('mirror_path', sa.Text(), nullable=True),
    sa.Column('blob', sa.LargeBinary(), nullable=True),
    sa.Column('pin_status', ENUM_TYPES["pin_status"], nullable=False),
    sa.Column('uploaded_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_media_uploaded_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_media')),
    sa.UniqueConstraint('sha256', name=op.f('uq_media_sha256'))
    )
    op.create_index('ix_media_cid', 'media', ['cid'], unique=False)
    op.create_index('ix_media_pin_status', 'media', ['pin_status'], unique=False)
    op.create_index('ix_media_uploaded_by', 'media', ['uploaded_by'], unique=False)
    op.create_table('oauth_identities',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('provider', ENUM_TYPES["oauth_provider"], nullable=False),
    sa.Column('provider_subject', sa.Text(), nullable=False),
    sa.Column('provider_email', postgresql.CITEXT(), nullable=True),
    sa.Column('email_verified', sa.Boolean(), nullable=False),
    sa.Column('linked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_oauth_identities_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_identities')),
    sa.UniqueConstraint('provider', 'provider_subject', name='uq_oauth_identities_provider_subject'),
    sa.UniqueConstraint('user_id', 'provider', name='uq_oauth_identities_user_provider')
    )
    op.create_index('ix_oauth_identities_user_id', 'oauth_identities', ['user_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('family_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replaced_by', sa.Uuid(), nullable=True),
    sa.Column('user_agent_hash', sa.String(length=64), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['replaced_by'], ['refresh_tokens.id'], name=op.f('fk_refresh_tokens_replaced_by_refresh_tokens'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    op.create_index('ix_refresh_tokens_expires_at', 'refresh_tokens', ['expires_at'], unique=False)
    op.create_index('ix_refresh_tokens_family_id', 'refresh_tokens', ['family_id'], unique=False)
    op.create_index('ix_refresh_tokens_replaced_by', 'refresh_tokens', ['replaced_by'], unique=False)
    op.create_index('ix_refresh_tokens_user_id_revoked_at', 'refresh_tokens', ['user_id', 'revoked_at'], unique=False)
    op.create_table('items',
    sa.Column('category_id', sa.Uuid(), nullable=False),
    sa.Column('category_schema_version', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Uuid(), nullable=True),
    sa.Column('registered_by', sa.Uuid(), nullable=False),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('quantity_unit', sa.Text(), nullable=False),
    sa.Column('item_hash', sa.Text(), nullable=False),
    sa.Column('tag_code', sa.Text(), nullable=True),
    sa.Column('status', ENUM_TYPES["item_status"], nullable=False),
    sa.Column('dispute_status', ENUM_TYPES["dispute_status"], nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['gi_categories.id'], name=op.f('fk_items_category_id_gi_categories'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_id'], ['items.id'], name=op.f('fk_items_parent_id_items'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['registered_by'], ['users.id'], name=op.f('fk_items_registered_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_items')),
    sa.UniqueConstraint('item_hash', name=op.f('uq_items_item_hash')),
    sa.UniqueConstraint('tag_code', name=op.f('uq_items_tag_code'))
    )
    op.create_index('ix_items_category_id_created_at', 'items', ['category_id', 'created_at'], unique=False)
    op.create_index('ix_items_created_at_id', 'items', ['created_at', 'id'], unique=False)
    op.create_index('ix_items_dispute_status', 'items', ['dispute_status'], unique=False)
    op.create_index('ix_items_parent_id', 'items', ['parent_id'], unique=False)
    op.create_index('ix_items_registered_by', 'items', ['registered_by'], unique=False)
    op.create_index('ix_items_status', 'items', ['status'], unique=False)
    op.create_index('ix_items_tag_code', 'items', ['tag_code'], unique=False)
    op.create_table('merkle_batches',
    sa.Column('root', sa.Text(), nullable=False),
    sa.Column('leaf_count', sa.Integer(), nullable=False),
    sa.Column('anchored_tx_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['anchored_tx_id'], ['chain_txs.id'], name=op.f('fk_merkle_batches_anchored_tx_id_chain_txs'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_merkle_batches')),
    sa.UniqueConstraint('root', name=op.f('uq_merkle_batches_root'))
    )
    op.create_index('ix_merkle_batches_anchored_tx_id', 'merkle_batches', ['anchored_tx_id'], unique=False)
    op.create_table('attestations',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('attestor_id', sa.Uuid(), nullable=False),
    sa.Column('attestor_role', ENUM_TYPES["user_role"], nullable=False),
    sa.Column('statement', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('statement_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['attestor_id'], ['users.id'], name=op.f('fk_attestations_attestor_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_attestations_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attestations')),
    sa.UniqueConstraint('item_id', 'attestor_id', name='uq_attestations_item_attestor')
    )
    op.create_index('ix_attestations_attestor_id', 'attestations', ['attestor_id'], unique=False)
    op.create_index('ix_attestations_item_id_created_at', 'attestations', ['item_id', 'created_at'], unique=False)
    op.create_index('ix_attestations_statement_hash', 'attestations', ['statement_hash'], unique=False)
    op.create_table('claims',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('device_fingerprint', sa.Text(), nullable=True),
    sa.Column('country_code', sa.String(length=2), nullable=True),
    sa.Column('region_code', sa.String(length=8), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_claims_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('item_id', name=op.f('pk_claims'))
    )
    op.create_index('ix_claims_claimed_at', 'claims', ['claimed_at'], unique=False)
    op.create_table('item_events',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('event_type', ENUM_TYPES["item_event_type"], nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_hash', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_item_events_actor_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_item_events_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item_events'))
    )
    op.create_index('ix_item_events_actor_id', 'item_events', ['actor_id'], unique=False)
    op.create_index('ix_item_events_event_type', 'item_events', ['event_type'], unique=False)
    op.create_index('ix_item_events_item_id_created_at', 'item_events', ['item_id', 'created_at'], unique=False)
    op.create_table('item_media',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('media_id', sa.Uuid(), nullable=False),
    sa.Column('kind', ENUM_TYPES["media_kind"], nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_item_media_item_id_items'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['media_id'], ['media.id'], name=op.f('fk_item_media_media_id_media'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('item_id', 'media_id', name=op.f('pk_item_media'))
    )
    op.create_index('ix_item_media_item_id_kind', 'item_media', ['item_id', 'kind'], unique=False)
    op.create_index('ix_item_media_media_id', 'item_media', ['media_id'], unique=False)
    op.create_table('merkle_leaves',
    sa.Column('batch_id', sa.Uuid(), nullable=False),
    sa.Column('leaf_index', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('leaf_hash', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['merkle_batches.id'], name=op.f('fk_merkle_leaves_batch_id_merkle_batches'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_merkle_leaves_item_id_items'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('batch_id', 'leaf_index', name=op.f('pk_merkle_leaves')),
    sa.UniqueConstraint('batch_id', 'item_id', name='uq_merkle_leaves_batch_item')
    )
    op.create_index('ix_merkle_leaves_item_id', 'merkle_leaves', ['item_id'], unique=False)
    op.create_table('scans',
    sa.Column('item_id', sa.Uuid(), nullable=False),
    sa.Column('tag_code', sa.Text(), nullable=False),
    sa.Column('country_code', sa.String(length=2), nullable=True),
    sa.Column('region_code', sa.String(length=8), nullable=True),
    sa.Column('device_fingerprint', sa.Text(), nullable=True),
    sa.Column('ip_hash', sa.String(length=64), nullable=True),
    sa.Column('suspicion_level', ENUM_TYPES["suspicion_level"], nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], name=op.f('fk_scans_item_id_items'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scans'))
    )
    op.create_index('ix_scans_created_at_id', 'scans', ['created_at', 'id'], unique=False)
    op.create_index('ix_scans_item_id_created_at', 'scans', ['item_id', 'created_at'], unique=False)
    op.create_index('ix_scans_suspicion_level', 'scans', ['suspicion_level'], unique=False)
    op.create_index('ix_scans_tag_code', 'scans', ['tag_code'], unique=False)


def downgrade() -> None:
    """Drop every table, then every enum type.

    Extensions are deliberately left in place: they are database-wide and may
    be in use by something outside this schema.
    """
    op.drop_index('ix_scans_tag_code', table_name='scans')
    op.drop_index('ix_scans_suspicion_level', table_name='scans')
    op.drop_index('ix_scans_item_id_created_at', table_name='scans')
    op.drop_index('ix_scans_created_at_id', table_name='scans')
    op.drop_table('scans')
    op.drop_index('ix_merkle_leaves_item_id', table_name='merkle_leaves')
    op.drop_table('merkle_leaves')
    op.drop_index('ix_item_media_media_id', table_name='item_media')
    op.drop_index('ix_item_media_item_id_kind', table_name='item_media')
    op.drop_table('item_media')
    op.drop_index('ix_item_events_item_id_created_at', table_name='item_events')
    op.drop_index('ix_item_events_event_type', table_name='item_events')
    op.drop_index('ix_item_events_actor_id', table_name='item_events')
    op.drop_table('item_events')
    op.drop_index('ix_claims_claimed_at', table_name='claims')
    op.drop_table('claims')
    op.drop_index('ix_attestations_statement_hash', table_name='attestations')
    op.drop_index('ix_attestations_item_id_created_at', table_name='attestations')
    op.drop_index('ix_attestations_attestor_id', table_name='attestations')
    op.drop_table('attestations')
    op.drop_index('ix_merkle_batches_anchored_tx_id', table_name='merkle_batches')
    op.drop_table('merkle_batches')
    op.drop_index('ix_items_tag_code', table_name='items')
    op.drop_index('ix_items_status', table_name='items')
    op.drop_index('ix_items_registered_by', table_name='items')
    op.drop_index('ix_items_parent_id', table_name='items')
    op.drop_index('ix_items_dispute_status', table_name='items')
    op.drop_index('ix_items_created_at_id', table_name='items')
    op.drop_index('ix_items_category_id_created_at', table_name='items')
    op.drop_table('items')
    op.drop_index('ix_refresh_tokens_user_id_revoked_at', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_replaced_by', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_family_id', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_expires_at', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index('ix_oauth_identities_user_id', table_name='oauth_identities')
    op.drop_table('oauth_identities')
    op.drop_index('ix_media_uploaded_by', table_name='media')
    op.drop_index('ix_media_pin_status', table_name='media')
    op.drop_index('ix_media_cid', table_name='media')
    op.drop_table('media')
    op.drop_index('ix_idempotency_keys_created_at', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')
    op.drop_index('ix_gi_categories_is_active', table_name='gi_categories')
    op.drop_index('ix_gi_categories_created_by', table_name='gi_categories')
    op.drop_table('gi_categories')
    op.drop_index('ix_dead_letters_source_created_at', table_name='dead_letters')
    op.drop_index('ix_dead_letters_resolved_by', table_name='dead_letters')
    op.drop_index('ix_dead_letters_resolved_at', table_name='dead_letters')
    op.drop_table('dead_letters')
    op.drop_index('ix_chain_txs_status', table_name='chain_txs')
    op.drop_index('ix_chain_txs_outbox_id', table_name='chain_txs')
    op.drop_index('ix_chain_txs_block_number', table_name='chain_txs')
    op.drop_table('chain_txs')
    op.drop_index('ix_auth_events_user_id_created_at', table_name='auth_events')
    op.drop_index('ix_auth_events_event_type_created_at', table_name='auth_events')
    op.drop_table('auth_events')
    op.drop_index('ix_users_status', table_name='users')
    op.drop_index('ix_users_role', table_name='users')
    op.drop_index('ix_users_created_at_id', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_rate_limit_buckets_expires_at', table_name='rate_limit_buckets')
    op.drop_table('rate_limit_buckets')
    op.drop_index('ix_quota_usage_name', table_name='quota_usage')
    op.drop_table('quota_usage')
    op.drop_index('ix_pending_tokens_expires_at', table_name='pending_tokens')
    op.drop_table('pending_tokens')
    op.drop_table('indexer_checkpoints')
    op.drop_index('ix_chain_outbox_status_next_attempt_at', table_name='chain_outbox')
    op.drop_index('ix_chain_outbox_locked_at', table_name='chain_outbox')
    op.drop_table('chain_outbox')
    op.drop_table('chain_nonce')

    # Types outlive their tables, so DROP TABLE alone leaves them behind and
    # the next upgrade fails on CREATE TYPE. Reverse order for symmetry.
    bind = op.get_bind()
    for name in reversed(ENUM_ORDER):
        ENUM_TYPES[name].drop(bind, checkfirst=True)
