"""SurrealDB HTTP client for flow runner."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_HOST = os.environ.get("SURREAL_HOST", "http://localhost:8282")
DEFAULT_NS = os.environ.get("SURREAL_NS", "flow_runner")
DEFAULT_DB = os.environ.get("SURREAL_DB", "main")
DEFAULT_USER = os.environ.get("SURREAL_USER", "root")
DEFAULT_PASS = os.environ.get("SURREAL_PASS", "root")


class SurrealClient:
    """Thin HTTP client for SurrealDB /sql endpoint."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        ns: str = DEFAULT_NS,
        db: str = DEFAULT_DB,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASS,
    ):
        self.host = host.rstrip("/")
        self.ns = ns
        self.db = db
        self._client = httpx.Client(
            base_url=self.host,
            auth=(user, password),
            headers={
                "surreal-ns": ns,
                "surreal-db": db,
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    def query(self, sql: str) -> list[dict[str, Any]]:
        """Execute SurrealQL and return result array."""
        resp = self._client.post("/sql", content=sql)
        resp.raise_for_status()
        data = resp.json()

        # SurrealDB returns array of statement results
        if isinstance(data, list):
            results = []
            for stmt in data:
                if stmt.get("status") == "ERR":
                    raise SurrealError(stmt.get("result", "Unknown DB error"))
                results.append(stmt.get("result"))
            return results

        raise SurrealError(f"Unexpected response format: {data}")

    def query_one(self, sql: str) -> Any:
        """Execute and return first statement's result."""
        results = self.query(sql)
        return results[0] if results else None

    def get_stored_query(self, key: str) -> dict[str, Any] | None:
        """Fetch a stored_query record by key."""
        result = self.query_one(
            f"SELECT * FROM stored_query WHERE key = '{key}' LIMIT 1;"
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def execute_stored_query(self, key: str, bind: dict[str, Any]) -> list[Any]:
        """Fetch a stored query by key, substitute params, execute."""
        sq = self.get_stored_query(key)
        if not sq:
            raise SurrealError(f"Stored query not found: {key}")

        sql = sq["sql"]
        for param, value in bind.items():
            # Convert Python None to SurrealDB NONE
            if value is None:
                sql = sql.replace(f"'${param}'", "NONE")
                sql = sql.replace(f"${param}", "NONE")
            else:
                sql = sql.replace(f"${param}", str(value))

        return self.query(sql)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SurrealError(Exception):
    pass
