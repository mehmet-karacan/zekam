-- Zekam baseline: gerekli eklentiler.
-- Bu betik yalnizca ilk kurulumda calisir. Sema olusturma islemi migration'lara aittir.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
