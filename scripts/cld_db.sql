-- 1. Create the dedicated user (if not already created)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'aisich_user') THEN
        CREATE USER aisich_user WITH PASSWORD 'aisich_dev_pw';
    END IF;
END
$$;

-- 2. Make aisich_user the owner of the existing database
ALTER DATABASE aisichdb OWNER TO aisich_user;
GRANT ALL PRIVILEGES ON DATABASE aisichdb TO aisich_user;

-- 3. Switch connection into aisichdb
\c aisichdb

-- 4. Transfer ownership of the public schema
-- Essential for PostgreSQL 15/16 where public CREATE rights are revoked by default
ALTER SCHEMA public OWNER TO aisich_user;
GRANT ALL ON SCHEMA public TO aisich_user;

-- 5. Grant rights on any tables or sequences that may already exist
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO aisich_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO aisich_user;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO aisich_user;

-- 6. Ensure any future tables or sequences created automatically inherit full rights
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO aisich_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO aisich_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO aisich_user;