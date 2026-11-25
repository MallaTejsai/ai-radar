"""PostgreSQL access: pooling, transactions and the run lock.

Supabase Postgres is reached with a standard connection string over TLS. There is
no in-memory fallback in production paths: if the database is unreachable the
pipeline fails loudly rather than pretending to remember things.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

import psycopg
from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..utils.logging import get_logger

logger = get_logger(__name__)


class DatabaseError(RuntimeError):
    """Raised when the database cannot be reached or a statement fails."""


def _reject_transaction_pooler(dsn: str) -> None:
    """Refuse a transaction-pooler DSN unless the operator opts in.

    Supabase's shared pooler in transaction mode (port 6543) does not support
    prepared statements, and it cannot hold a session-scoped advisory lock: the
    next statement may run on a different backend, so ``pg_advisory_unlock`` can
    miss the connection that owns the lock. This project relies on both, so a
    6543 DSN would fail with prepared-statement errors and leak the run lock,
    making every later run exit ``skipped_locked``.

    Session mode on the same shared pooler (port 5432) supports both and is
    reachable over IPv4, which GitHub-hosted runners require.
    """
    if os.environ.get("AI_RADAR_ALLOW_TRANSACTION_POOLER") == "1":
        return
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return
    if parts.scheme not in ("postgres", "postgresql"):
        return
    if parts.port != 6543:
        return
    raise DatabaseError(
        "port 6543 is a transaction-mode pooler: prepared statements and session "
        "advisory locks are not supported there. Use the Supabase shared pooler in "
        "session mode (port 5432) or the direct connection. Set "
        "AI_RADAR_ALLOW_TRANSACTION_POOLER=1 only if you have disabled prepared "
        "statements and replaced the run lock."
    )


PLACEHOLDER_HINT = (
    "The usual cause is an unreplaced provider placeholder: Supabase's Connect panel "
    "copies the URI with [YOUR-PASSWORD] in place of the password, and those brackets "
    "make the URI unparseable. Replace it with the database password."
)


def validate_dsn(dsn: str | None, *, label: str = "DATABASE_URL") -> str:
    """Validate a database DSN before any connection is attempted.

    A malformed URI must fail here, not inside libpq: libpq echoes the unparsable
    netloc in its error, which can include a fragment of the password. The messages
    below never include the DSN, and ``label`` names the variable the operator
    actually set, so a staging problem does not blame production.
    """
    if not dsn:
        raise DatabaseError(f"{label} is not configured")
    try:
        parts = urlsplit(dsn)
        _ = parts.port  # raises ValueError on a malformed port
        host = parts.hostname or ""
    except ValueError as exc:
        raise DatabaseError(
            f"{label} is malformed. {PLACEHOLDER_HINT} If the password itself contains "
            "[ ] @ : / ? # & or a space, percent-encode it (e.g. [ -> %5B, ] -> %5D, "
            "@ -> %40) or reset the database password to letters and digits only."
        ) from exc
    if parts.scheme not in ("postgres", "postgresql"):
        raise DatabaseError(f"{label} must start with postgresql://")
    if not host or any(character in host for character in "@[] "):
        raise DatabaseError(
            f"{label} is malformed: the host contains characters that belong to "
            "the password. Percent-encode reserved characters, or reset the password "
            "to letters and digits only."
        )
    if parts.password is None:
        raise DatabaseError(
            f"{label} has no password. Copy the full session-pooler URI from "
            "Supabase -> Connect and replace the password placeholder."
        )
    _reject_transaction_pooler(dsn)
    return dsn


#: Substrings that mark a database as throwaway. "stag" is included so a
#: Supabase project such as ``ai-radar-staging`` qualifies; the production
#: database is named ``postgres`` and therefore never qualifies.
DISPOSABLE_NAME_TAGS = ("test", "ci", "scratch", "sandbox", "stag")


def is_disposable_database(dsn: str) -> bool:
    """Whether the target name looks like a throwaway database.

    The destructive integration suite truncates tables, and a staging run must
    never write to production; this guard stops both from touching a real project
    that someone pointed a variable at by mistake.
    """
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return False
    name = (parts.path or "").lstrip("/").split("?")[0].lower()
    return any(tag in name for tag in DISPOSABLE_NAME_TAGS)


def endpoint_signature(dsn: str) -> tuple[str, str, str]:
    """Identify the server a DSN reaches: (host, port, user).

    Deliberately excludes the database name: Supabase names every database
    ``postgres``, so two different Supabase projects differ in host and user (the
    project ref), never in the database name.
    """
    try:
        parts = urlsplit(dsn)
        return (
            (parts.hostname or "").lower(),
            str(parts.port or ""),
            parts.username or "",
        )
    except ValueError as exc:
        raise DatabaseError(
            f"Cannot parse the database URL. {PLACEHOLDER_HINT}"
        ) from exc


def masked_endpoint(dsn: str) -> str:
    """Describe where a DSN points, with the password removed.

    Example: ``postgres.stagingref@aws-0-ap-south-1.pooler.supabase.com:5432/postgres``.

    Safe to print and to paste into a report: the password never appears, and the
    rest is what a person needs in order to confirm which project a run will use.
    """
    if not dsn:
        return "(not set)"
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        database = (parts.path or "").lstrip("/")
        user = parts.username or ""
    except ValueError:
        # Never let reporting crash: an unparseable URL is a symptom to report.
        return "(unparseable DSN - password placeholder not replaced?)"
    return f"{user}@{host}{port}/{database}" if user else f"{host}{port}/{database}"


def dsn_fingerprint(dsn: str) -> str:
    """A short, non-reversible fingerprint of a DSN, for operator confirmation.

    Two runs agree on the fingerprint only when they use the same string, so it
    proves "this is the URL I set" without revealing any part of it.
    """
    if not dsn:
        return "(none)"
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:16]


def assert_staging_target(
    staging_dsn: str,
    *,
    staging: bool,
    production_dsn: str = "",
) -> None:
    """Refuse a staging run that could reach the production database.

    Staging mode never reads ``DATABASE_URL``; it reads a dedicated
    ``AI_RADAR_STAGING_DATABASE_URL``. That is the primary guarantee: a forgotten
    override cannot fall back to production, because production is never read.

    Two further checks close the copy/paste gap:

    * the staging URL must be present;
    * it must not reach the same server as ``DATABASE_URL``. Supabase database
      names are always ``postgres``, so identity is host+port+user (the project
      ref), not the database name.

    When no production DSN is known (a machine that only ever sees staging), the
    database name must look disposable instead.

    Messages never contain the DSN.
    """
    if not staging:
        return
    if not staging_dsn:
        raise DatabaseError(
            "AI_RADAR_STAGING=1 requires AI_RADAR_STAGING_DATABASE_URL to point at "
            "a disposable staging database, but it is not set. Production is never "
            "read in staging mode."
        )
    if production_dsn:
        if endpoint_signature(staging_dsn) == endpoint_signature(production_dsn):
            raise DatabaseError(
                "AI_RADAR_STAGING=1 refuses this staging DATABASE_URL: it reaches the "
                "same server and user as DATABASE_URL, so it is the production "
                "project (Supabase names every database 'postgres'). Use a separate "
                "staging project."
            )
        return
    if not is_disposable_database(staging_dsn):
        raise DatabaseError(
            "AI_RADAR_STAGING=1 cannot verify this staging DATABASE_URL: there is no "
            "DATABASE_URL to compare against, and the database name does not contain "
            "test, ci, scratch, sandbox or stag."
        )


class Database:
    """Thin async wrapper around a bounded connection pool."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 6,
        connect_timeout: float = 20.0,
    ) -> None:
        validate_dsn(dsn)
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._connect_timeout = connect_timeout
        self._pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            # Supabase's pooler needs a short keepalive; a long-idle connection is
            # routinely dropped server-side.
            kwargs={"application_name": "ai-radar", "connect_timeout": int(self._connect_timeout)},
            timeout=self._connect_timeout,
        )
        try:
            await self._pool.open(wait=True, timeout=self._connect_timeout)
        except Exception as exc:  # noqa: BLE001
            await self._pool.close()
            self._pool = None
            raise DatabaseError(f"could not connect to PostgreSQL: {exc}") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise DatabaseError("database is not connected - call connect() first")
        return self._pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncCursor[dict]]:
        """Yield a dict cursor inside a transaction that commits on success."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncConnection]:
        """Hold one connection open (needed for session-scoped advisory locks)."""
        async with self.pool.connection() as conn:
            yield conn

    async def ping(self) -> bool:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("select 1")
                    row = await cursor.fetchone()
                    return bool(row and row[0] == 1)
        except (psycopg.Error, DatabaseError) as exc:
            logger.error("database_ping_failed", error=str(exc))
            return False

    async def server_time(self):
        async with self.pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("select now()")
                row = await cursor.fetchone()
                return row[0] if row else None


class RunLock:
    """Session-scoped PostgreSQL advisory lock.

    GitHub Actions can start two runs within seconds of each other (schedule plus
    manual dispatch, or a delayed run overlapping the next slot). The lock makes
    the second run exit immediately instead of double-sending notifications.

    The lock is held on one server backend for the whole run, so the database must
    be reached in session mode (shared pooler port 5432 or a direct connection),
    never through a transaction pooler.
    """

    def __init__(self, database: Database, name: str, *, timeout_seconds: int = 20) -> None:
        self._database = database
        self._name = name
        self._timeout = timeout_seconds
        self._conn: AsyncConnection | None = None
        self.acquired = False

    async def __aenter__(self) -> "RunLock":
        self._conn = await self._database.pool.getconn()
        deadline = asyncio.get_running_loop().time() + max(1, self._timeout)
        try:
            while True:
                async with self._conn.cursor() as cursor:
                    await cursor.execute(
                        "select pg_try_advisory_lock(hashtext(%s))", (self._name,)
                    )
                    row = await cursor.fetchone()
                    acquired = bool(row and row[0])
                if acquired:
                    self.acquired = True
                    logger.info("run_lock_acquired", lock=self._name)
                    return self
                if asyncio.get_running_loop().time() >= deadline:
                    logger.warning("run_lock_busy", lock=self._name)
                    return self
                await asyncio.sleep(1.0)
        except Exception:
            await self._release_connection()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            try:
                if self.acquired:
                    async with self._conn.cursor() as cursor:
                        await cursor.execute(
                            "select pg_advisory_unlock(hashtext(%s))", (self._name,)
                        )
                    self.acquired = False
            finally:
                await self._release_connection()

    async def _release_connection(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await self._database.pool.putconn(conn)
