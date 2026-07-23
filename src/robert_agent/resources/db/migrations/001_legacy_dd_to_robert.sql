CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

INSERT INTO schema_migrations(version, name, checksum, applied_at)
VALUES (
  2,
  'legacy-dd-to-robert',
  'legacy-dd-to-robert-v1',
  datetime('now')
);
