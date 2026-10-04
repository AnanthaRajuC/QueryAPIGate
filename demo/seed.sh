#!/bin/sh
# Seeds the one-command demo through the Management API, once queryapigate is healthy: the demo-postgres connection
# and two saved queries. Safe to re-run - anything that already exists answers 409 and is left as it is.
set -e
API=http://queryapigate:5000/api/v1
KEY="X-API-Key: ${QUERYAPIGATE_API_KEY}"

post() {
    status=$(curl -s -o /tmp/out -w '%{http_code}' -X POST "$API/$1" -H "$KEY" -H 'Content-Type: application/json' -d "$2")
    case "$status" in
        201) echo "created $1: $3" ;;
        409) echo "already there $1: $3" ;;
        *) echo "failed $1: $3 ($status)"; cat /tmp/out; exit 1 ;;
    esac
}

post connections '{"name": "demo-postgres", "db": "postgres", "host": "db", "port": 5432, "database": "postgres",
  "user": "postgres", "password": "${DEMO_DB_PASSWORD}", "active": true}' demo-postgres

post queries '{"name": "films_by_rating", "connection_name": "demo-postgres", "publish": true,
  "description": "Films with a given rating up to a maximum length (minutes)", "tags": ["demo"],
  "sql": "SELECT film_id, title, rating, length FROM films WHERE rating = :rating AND length <= :max_length ORDER BY title",
  "parameters": {
    "rating": {"type": "str", "enum": ["G", "PG", "PG-13", "R", "NC-17"], "default": "PG", "description": "MPAA rating"},
    "max_length": {"type": "int", "min": 1, "max": 600, "default": 120, "description": "Longest film to include, in minutes"}}}' films_by_rating

post queries '{"name": "film_by_id", "connection_name": "demo-postgres", "publish": true,
  "description": "Look up one film by id", "tags": ["demo"],
  "sql": "SELECT film_id, title, rating, length FROM films WHERE film_id = :id",
  "parameters": {"id": {"type": "int", "min": 1, "description": "Film id"}}}' film_by_id
