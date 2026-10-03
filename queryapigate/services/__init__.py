"""Application services behind the Management API (ADR 0001): what an operation means, independent of HTTP.

Routes (v1.py) translate requests to calls here and results to responses; the store (store.py) keeps the data.
Grown one resource at a time as /api/v1 is built (BACKLOG #72), rather than reorganised up front.
"""
