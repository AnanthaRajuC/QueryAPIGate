-- The Iceberg catalog's database (Trino creates its tables), and `crm`: an account manager and a contact for each of
-- TPC-H's 1,500 customers, so a query can join the lakehouse with an operational database.
CREATE DATABASE iceberg;
CREATE DATABASE crm;

-- The JDBC catalog's two tables, which Trino doesn't create itself
\connect iceberg
CREATE TABLE iceberg_namespace_properties (
    catalog_name varchar(255) NOT NULL,
    namespace varchar(255) NOT NULL,
    property_key varchar(255),
    property_value varchar(1000),
    PRIMARY KEY (catalog_name, namespace, property_key)
);
CREATE TABLE iceberg_tables (
    catalog_name varchar(255) NOT NULL,
    table_namespace varchar(255) NOT NULL,
    table_name varchar(255) NOT NULL,
    metadata_location varchar(1000),
    previous_metadata_location varchar(1000),
    iceberg_type varchar(5),
    PRIMARY KEY (catalog_name, table_namespace, table_name)
);

\connect crm
CREATE TABLE accounts (
    custkey integer PRIMARY KEY,
    account_manager text NOT NULL,
    contact_email text NOT NULL,
    tier text NOT NULL
);
INSERT INTO accounts
SELECT n,
       (ARRAY['Asha Rao', 'Ben Okafor', 'Chen Wei', 'Dana Novak', 'Emil Larsen'])[1 + n % 5],
       'buyer' || n || '@customer' || (n % 97) || '.example.com',
       CASE WHEN n % 10 = 1 THEN 'gold' WHEN n % 4 = 1 THEN 'silver' ELSE 'standard' END
FROM generate_series(1, 1500) AS n;
