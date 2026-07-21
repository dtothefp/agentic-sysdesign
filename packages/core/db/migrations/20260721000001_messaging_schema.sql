-- migrate:up

CREATE TABLE msg_users (
    id          BIGSERIAL PRIMARY KEY,
    external_id TEXT UNIQUE,              -- Supabase auth `sub`, populated in Phase 5
    display_name TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE msg_conversations (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Join table. Groups are absorbed into the conversation abstraction rather than
-- getting their own table: a DM is a conversation with two participants, a group
-- is one with N. Endorsed by Garett McCann in the 2026-07-13 round.
CREATE TABLE msg_participants (
    conversation_id      BIGINT NOT NULL REFERENCES msg_conversations(id) ON DELETE CASCADE,
    user_id              BIGINT NOT NULL REFERENCES msg_users(id) ON DELETE CASCADE,
    joined_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_read_message_id BIGINT,          -- unread counts without a receipts table
    PRIMARY KEY (conversation_id, user_id)
);

-- "list my conversations" reads by user, and the PK is ordered the other way.
CREATE INDEX msg_participants_user_idx ON msg_participants (user_id);

CREATE TABLE msg_messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES msg_conversations(id) ON DELETE CASCADE,
    sender_id       BIGINT NOT NULL REFERENCES msg_users(id),
    body            TEXT NOT NULL,
    client_msg_id   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- GAP 2a: the idempotency key. The NFR "reliable delivery => retries" is cashed HERE.
-- A client that retries a send reuses client_msg_id; the second INSERT conflicts and
-- the API returns the original row instead of duplicating the message.
CREATE UNIQUE INDEX msg_messages_idem_idx ON msg_messages (sender_id, client_msg_id);

-- GAP 2b: the scroll-back index. Cursor pagination ("messages before id X") rides this.
-- Ordered by id, not created_at: created_at collides under concurrency and is subject to
-- clock skew across instances, while a sequence is monotonic within one database.
CREATE INDEX msg_messages_conv_id_idx ON msg_messages (conversation_id, id DESC);

-- Two seeded users so Phases 1-4 can run on `X-User-Id` with no auth layer.
INSERT INTO msg_users (display_name) VALUES ('David'), ('Test Contact');
INSERT INTO msg_conversations DEFAULT VALUES;
INSERT INTO msg_participants (conversation_id, user_id) VALUES (1, 1), (1, 2);

-- migrate:down
DROP TABLE IF EXISTS msg_messages;
DROP TABLE IF EXISTS msg_participants;
DROP TABLE IF EXISTS msg_conversations;
DROP TABLE IF EXISTS msg_users;
