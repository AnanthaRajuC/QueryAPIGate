-- The lakehouse: Iceberg tables written from Trino's built-in TPC-H data (the `tiny` scale: 15,000 orders), stored as
-- Parquet in the `lakehouse` bucket. Orders are partitioned by month, so a query for one year reads that year's files.
-- Safe to run again: each table is made only if it isn't there.
CREATE SCHEMA IF NOT EXISTS iceberg.sales;

CREATE TABLE IF NOT EXISTS iceberg.sales.orders
WITH (format = 'PARQUET', partitioning = ARRAY['month(orderdate)'])
AS SELECT orderkey, custkey, orderstatus, totalprice, orderdate, orderpriority FROM tpch.tiny.orders;

CREATE TABLE IF NOT EXISTS iceberg.sales.customers
WITH (format = 'PARQUET')
AS SELECT c.custkey, c.name, c.mktsegment, n.name AS nation, r.name AS region
   FROM tpch.tiny.customer c
   JOIN tpch.tiny.nation n ON c.nationkey = n.nationkey
   JOIN tpch.tiny.region r ON n.regionkey = r.regionkey;

SELECT count(*) AS orders FROM iceberg.sales.orders;
