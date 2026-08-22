CREATE DATABASE evolution;
CREATE DATABASE aisichdb;

CREATE USER evolution_user WITH PASSWORD 'evolution_dev_pw';
CREATE USER aisich_user WITH PASSWORD 'aisich_dev_pw';

GRANT ALL PRIVILEGES ON DATABASE evolution TO evolution_user;
GRANT ALL PRIVILEGES ON DATABASE aisichdb TO aisich_user;

\c evolution
GRANT ALL ON SCHEMA public TO evolution_user;
\c aisichdb
GRANT ALL ON SCHEMA public TO aisich_user;