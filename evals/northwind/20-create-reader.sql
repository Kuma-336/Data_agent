CREATE ROLE northwind_reader LOGIN PASSWORD 'northwind_reader';
GRANT CONNECT ON DATABASE northwind TO northwind_reader;
GRANT USAGE ON SCHEMA public TO northwind_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO northwind_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO northwind_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO northwind_reader;
ALTER ROLE northwind_reader SET default_transaction_read_only = on;
