CREATE TABLE auth_schema_version (
				id INTEGER PRIMARY KEY CHECK (id = 1),
				version INTEGER NOT NULL
			);
CREATE TABLE cache (
				key TEXT PRIMARY KEY,
				value TEXT NOT NULL,
				expires_at INTEGER NOT NULL
			);
CREATE INDEX idx_cache_expires ON cache(expires_at);
CREATE TABLE usage_history (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				recorded_at INTEGER NOT NULL,
				provider TEXT NOT NULL,
				account_key TEXT NOT NULL,
				email TEXT,
				account_id TEXT,
				limit_id TEXT NOT NULL,
				label TEXT NOT NULL,
				window_label TEXT,
				used_fraction REAL,
				status TEXT,
				resets_at INTEGER
			);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_usage_history_series ON usage_history(provider, account_key, limit_id, recorded_at);
CREATE INDEX idx_usage_history_recorded ON usage_history(recorded_at);
CREATE TABLE auth_credentials (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				provider TEXT NOT NULL,
				credential_type TEXT NOT NULL,
				data TEXT NOT NULL,
				disabled_cause TEXT DEFAULT NULL,
				identity_key TEXT DEFAULT NULL,
				created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
				updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
			);
CREATE INDEX idx_auth_provider ON auth_credentials(provider);
CREATE INDEX idx_auth_provider_identity ON auth_credentials(provider, identity_key) WHERE identity_key IS NOT NULL;
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE settings (
	key TEXT PRIMARY KEY,
	value TEXT NOT NULL,
	updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE model_usage (
	model_key TEXT PRIMARY KEY,
	last_used_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE threads (
	id TEXT PRIMARY KEY,
	updated_at INTEGER NOT NULL,
	rollout_path TEXT NOT NULL,
	cwd TEXT NOT NULL,
	source_kind TEXT NOT NULL
);
CREATE TABLE stage1_outputs (
	thread_id TEXT PRIMARY KEY,
	source_updated_at INTEGER NOT NULL,
	raw_memory TEXT NOT NULL,
	rollout_summary TEXT NOT NULL,
	rollout_slug TEXT,
	generated_at INTEGER NOT NULL
);
CREATE TABLE jobs (
	kind TEXT NOT NULL,
	job_key TEXT NOT NULL,
	status TEXT NOT NULL,
	worker_id TEXT,
	ownership_token TEXT,
	started_at INTEGER,
	finished_at INTEGER,
	lease_until INTEGER,
	retry_at INTEGER,
	retry_remaining INTEGER NOT NULL,
	last_error TEXT,
	input_watermark INTEGER,
	last_success_watermark INTEGER,
	PRIMARY KEY (kind, job_key)
);
CREATE TABLE usage_cost_history (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				recorded_at INTEGER NOT NULL,
				provider TEXT NOT NULL,
				account_key TEXT NOT NULL,
				cost_usd REAL NOT NULL
			);
CREATE INDEX idx_usage_cost_history_lookup ON usage_cost_history(provider, account_key, recorded_at);
CREATE TABLE auth_credential_blocks (
				credential_id INTEGER NOT NULL,
				provider_key TEXT NOT NULL,
				block_scope TEXT NOT NULL DEFAULT '',
				blocked_until_ms INTEGER NOT NULL,
				updated_at INTEGER NOT NULL,
				PRIMARY KEY (credential_id, provider_key, block_scope)
			);
CREATE INDEX idx_auth_credential_blocks_expires ON auth_credential_blocks(blocked_until_ms);
CREATE TABLE auth_credential_refresh_leases (
				credential_id INTEGER PRIMARY KEY,
				owner TEXT NOT NULL,
				expires_at_ms INTEGER NOT NULL,
				updated_at INTEGER NOT NULL
			);
CREATE INDEX idx_auth_credential_refresh_leases_expires ON auth_credential_refresh_leases(expires_at_ms);
CREATE TABLE model_perf (
	model_key TEXT PRIMARY KEY,
	samples REAL NOT NULL DEFAULT 0,
	output_tokens REAL NOT NULL DEFAULT 0,
	gen_ms REAL NOT NULL DEFAULT 0,
	ttft_samples REAL NOT NULL DEFAULT 0,
	ttft_ms REAL NOT NULL DEFAULT 0,
	updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE meta (
	key TEXT PRIMARY KEY,
	value TEXT NOT NULL
);
CREATE TABLE clients (
				install_id TEXT PRIMARY KEY,
				hostname TEXT,
				first_seen INTEGER NOT NULL,
				last_seen INTEGER NOT NULL
			);
CREATE TABLE client_usage (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				recorded_at INTEGER NOT NULL,
				install_id TEXT NOT NULL,
				provider TEXT NOT NULL,
				model TEXT NOT NULL,
				requests INTEGER NOT NULL,
				input_tokens INTEGER NOT NULL,
				output_tokens INTEGER NOT NULL,
				cache_read_tokens INTEGER NOT NULL,
				cache_write_tokens INTEGER NOT NULL,
				cost_usd REAL NOT NULL DEFAULT 0
			, app TEXT NOT NULL DEFAULT '');
CREATE INDEX idx_client_usage_series ON client_usage(install_id, provider, model, recorded_at);
CREATE INDEX idx_client_usage_recorded ON client_usage(recorded_at);
CREATE TABLE auth_credential_block_mirror_guard (
				credential_id INTEGER PRIMARY KEY
			) WITHOUT ROWID;
CREATE TRIGGER auth_codex_shared_insert_to_meters
				AFTER INSERT ON auth_credential_blocks
				WHEN NEW.provider_key = 'openai-codex:oauth'
					AND NEW.block_scope = 'shared'
					AND NOT EXISTS (
						SELECT 1 FROM auth_credential_block_mirror_guard
						WHERE credential_id = NEW.credential_id
					)
				BEGIN
					INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
					VALUES (NEW.credential_id);
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					VALUES (
						NEW.credential_id,
						NEW.provider_key,
						'chat',
						NEW.blocked_until_ms,
						NEW.updated_at
					)
					ON CONFLICT(credential_id, provider_key, block_scope) DO UPDATE SET
						blocked_until_ms = MAX(auth_credential_blocks.blocked_until_ms, excluded.blocked_until_ms),
						updated_at = MAX(auth_credential_blocks.updated_at, excluded.updated_at);
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					VALUES (
						NEW.credential_id,
						NEW.provider_key,
						'spark',
						NEW.blocked_until_ms,
						NEW.updated_at
					)
					ON CONFLICT(credential_id, provider_key, block_scope) DO UPDATE SET
						blocked_until_ms = MAX(auth_credential_blocks.blocked_until_ms, excluded.blocked_until_ms),
						updated_at = MAX(auth_credential_blocks.updated_at, excluded.updated_at);
					DELETE FROM auth_credential_block_mirror_guard
					WHERE credential_id = NEW.credential_id;
				END;
CREATE TRIGGER auth_codex_meter_insert_to_shared
				AFTER INSERT ON auth_credential_blocks
				WHEN NEW.provider_key = 'openai-codex:oauth'
					AND NEW.block_scope IN ('chat', 'spark')
					AND NOT EXISTS (
						SELECT 1 FROM auth_credential_block_mirror_guard
						WHERE credential_id = NEW.credential_id
					)
				BEGIN
					INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
					VALUES (NEW.credential_id);
					DELETE FROM auth_credential_blocks
					WHERE credential_id = NEW.credential_id
						AND provider_key = NEW.provider_key
						AND block_scope = 'shared';
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					SELECT
						NEW.credential_id,
						NEW.provider_key,
						'shared',
						MAX(blocked_until_ms),
						MAX(updated_at)
					FROM auth_credential_blocks
					WHERE credential_id = NEW.credential_id
						AND provider_key = NEW.provider_key
						AND block_scope IN ('chat', 'spark')
					GROUP BY credential_id, provider_key;
					DELETE FROM auth_credential_block_mirror_guard
					WHERE credential_id = NEW.credential_id;
				END;
CREATE TRIGGER auth_codex_shared_update_to_meters
				AFTER UPDATE ON auth_credential_blocks
				WHEN NEW.provider_key = 'openai-codex:oauth'
					AND NEW.block_scope = 'shared'
					AND NOT EXISTS (
						SELECT 1 FROM auth_credential_block_mirror_guard
						WHERE credential_id = NEW.credential_id
					)
				BEGIN
					INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
					VALUES (NEW.credential_id);
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					VALUES (
						NEW.credential_id,
						NEW.provider_key,
						'chat',
						NEW.blocked_until_ms,
						NEW.updated_at
					)
					ON CONFLICT(credential_id, provider_key, block_scope) DO UPDATE SET
						blocked_until_ms = MAX(auth_credential_blocks.blocked_until_ms, excluded.blocked_until_ms),
						updated_at = MAX(auth_credential_blocks.updated_at, excluded.updated_at);
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					VALUES (
						NEW.credential_id,
						NEW.provider_key,
						'spark',
						NEW.blocked_until_ms,
						NEW.updated_at
					)
					ON CONFLICT(credential_id, provider_key, block_scope) DO UPDATE SET
						blocked_until_ms = MAX(auth_credential_blocks.blocked_until_ms, excluded.blocked_until_ms),
						updated_at = MAX(auth_credential_blocks.updated_at, excluded.updated_at);
					DELETE FROM auth_credential_block_mirror_guard
					WHERE credential_id = NEW.credential_id;
				END;
CREATE TRIGGER auth_codex_meter_update_to_shared
				AFTER UPDATE ON auth_credential_blocks
				WHEN NEW.provider_key = 'openai-codex:oauth'
					AND NEW.block_scope IN ('chat', 'spark')
					AND NOT EXISTS (
						SELECT 1 FROM auth_credential_block_mirror_guard
						WHERE credential_id = NEW.credential_id
					)
				BEGIN
					INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
					VALUES (NEW.credential_id);
					DELETE FROM auth_credential_blocks
					WHERE credential_id = NEW.credential_id
						AND provider_key = NEW.provider_key
						AND block_scope = 'shared';
					INSERT INTO auth_credential_blocks (
						credential_id,
						provider_key,
						block_scope,
						blocked_until_ms,
						updated_at
					)
					SELECT
						NEW.credential_id,
						NEW.provider_key,
						'shared',
						MAX(blocked_until_ms),
						MAX(updated_at)
					FROM auth_credential_blocks
					WHERE credential_id = NEW.credential_id
						AND provider_key = NEW.provider_key
						AND block_scope IN ('chat', 'spark')
					GROUP BY credential_id, provider_key;
					DELETE FROM auth_credential_block_mirror_guard
					WHERE credential_id = NEW.credential_id;
				END;
CREATE TRIGGER auth_codex_shared_delete_to_meters
			AFTER DELETE ON auth_credential_blocks
			WHEN OLD.provider_key = 'openai-codex:oauth'
				AND OLD.block_scope = 'shared'
				AND NOT EXISTS (
					SELECT 1 FROM auth_credential_block_mirror_guard
					WHERE credential_id = OLD.credential_id
				)
			BEGIN
				INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
				VALUES (OLD.credential_id);
				DELETE FROM auth_credential_blocks
				WHERE credential_id = OLD.credential_id
					AND provider_key = OLD.provider_key
					AND block_scope IN ('chat', 'spark');
				DELETE FROM auth_credential_block_mirror_guard
				WHERE credential_id = OLD.credential_id;
			END;
CREATE TRIGGER auth_codex_meter_delete_to_shared
			AFTER DELETE ON auth_credential_blocks
			WHEN OLD.provider_key = 'openai-codex:oauth'
				AND OLD.block_scope IN ('chat', 'spark')
				AND NOT EXISTS (
					SELECT 1 FROM auth_credential_block_mirror_guard
					WHERE credential_id = OLD.credential_id
				)
			BEGIN
				INSERT OR IGNORE INTO auth_credential_block_mirror_guard (credential_id)
				VALUES (OLD.credential_id);
				DELETE FROM auth_credential_blocks
				WHERE credential_id = OLD.credential_id
					AND provider_key = OLD.provider_key
					AND block_scope = 'shared';
				INSERT INTO auth_credential_blocks (
					credential_id,
					provider_key,
					block_scope,
					blocked_until_ms,
					updated_at
				)
				SELECT
					OLD.credential_id,
					OLD.provider_key,
					'shared',
					MAX(blocked_until_ms),
					MAX(updated_at)
				FROM auth_credential_blocks
				WHERE credential_id = OLD.credential_id
					AND provider_key = OLD.provider_key
					AND block_scope IN ('chat', 'spark')
				GROUP BY credential_id, provider_key;
				DELETE FROM auth_credential_block_mirror_guard
				WHERE credential_id = OLD.credential_id;
			END;
CREATE TABLE auth_change_revision (
				id INTEGER PRIMARY KEY CHECK (id = 1),
				revision INTEGER NOT NULL
			);
CREATE TRIGGER auth_change_revision_auth_credentials_insert
					AFTER INSERT ON auth_credentials
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TRIGGER auth_change_revision_auth_credentials_update
					AFTER UPDATE ON auth_credentials
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TRIGGER auth_change_revision_auth_credentials_delete
					AFTER DELETE ON auth_credentials
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TRIGGER auth_change_revision_auth_credential_blocks_insert
					AFTER INSERT ON auth_credential_blocks
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TRIGGER auth_change_revision_auth_credential_blocks_update
					AFTER UPDATE ON auth_credential_blocks
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TRIGGER auth_change_revision_auth_credential_blocks_delete
					AFTER DELETE ON auth_credential_blocks
					BEGIN
						UPDATE auth_change_revision SET revision = revision + 1 WHERE id = 1;
					END;
CREATE TABLE command_usage (
	name TEXT PRIMARY KEY,
	count INTEGER NOT NULL DEFAULT 0,
	last_used_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE hint_usage (
	name TEXT PRIMARY KEY,
	count INTEGER NOT NULL DEFAULT 0,
	last_used_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE TABLE notice_claims (
	key TEXT PRIMARY KEY,
	expires_at_ms INTEGER NOT NULL
);
