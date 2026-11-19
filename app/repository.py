from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from json import loads
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

FAQ_CACHE_LIMIT = 16


class Repository:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @asynccontextmanager
    async def tenant(self, store_id: UUID) -> AsyncIterator[AsyncConnection]:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("select set_config('app.store_id', %s, true)", (str(store_id),))
            yield conn

    async def faq(
        self, store_id: UUID, locale: str, topic: str, vector: list[float], model: str
    ) -> tuple[str, float, float | None] | None:
        parameter = "[" + ",".join(map(str, vector)) + "]"
        async with self.tenant(store_id) as conn:
            cursor = await conn.execute("""
                select answer, 1 - (embedding OPERATOR(extensions.<=>) %s::extensions.vector) as score
                from public.faqs
                where store_id = %s and locale = %s and topic = %s and embedding_model = %s
                    and (embedding OPERATOR(extensions.<#>) embedding) < 0
                order by embedding OPERATOR(extensions.<=>) %s::extensions.vector
                limit 2
            """, (parameter, store_id, locale, topic, model, parameter))
            rows = await cursor.fetchall()
        return (rows[0][0], rows[0][1], rows[1][1] if len(rows) > 1 else None) if rows else None

    async def faq_bucket(
        self, store_id: UUID, locale: str, topic: str, model: str
    ) -> list[tuple[str, list[float]]] | None:
        """Return the entire small policy bucket, or None if it exceeds the cache limit."""
        async with self.tenant(store_id) as conn:
            cursor = await conn.execute("""
                select answer, embedding::text
                from public.faqs
                where store_id = %s and locale = %s and topic = %s and embedding_model = %s
                    and (embedding OPERATOR(extensions.<#>) embedding) < 0
                limit %s
            """, (store_id, locale, topic, model, FAQ_CACHE_LIMIT + 1))
            rows = await cursor.fetchall()
        if len(rows) > FAQ_CACHE_LIMIT:
            return None
        return [(answer, loads(vector)) for answer, vector in rows]

    async def handoff(self, store_id: UUID, event: str, sender: str, reason: str) -> None:
        async with self.tenant(store_id) as conn:
            await conn.execute("""
                insert into public.handoffs (store_id, event_id, sender, reason)
                values (%s, %s, %s, %s) on conflict (store_id, event_id) do nothing
            """, (store_id, event, sender, reason))

    async def claim_action(
        self, store_id: UUID, token: str, kind: str, order_id: str, line_id: str | None
    ) -> str:
        async with self.tenant(store_id) as conn:
            cursor = await conn.execute("""
                insert into public.shopify_actions (store_id, token, kind, order_id, line_id)
                values (%s, %s, %s, %s, %s) on conflict do nothing returning token
            """, (store_id, token, kind, order_id, line_id))
            if await cursor.fetchone():
                return "claimed"
            cursor = await conn.execute("""
                select provider_id from public.shopify_actions
                where store_id = %s and kind = %s and order_id = %s
                    and line_id is not distinct from %s
            """, (store_id, kind, order_id, line_id))
            row = await cursor.fetchone()
            return "done" if row and row[0] else "review"

    async def finish_action(self, store_id: UUID, token: str, provider_id: str) -> None:
        async with self.tenant(store_id) as conn:
            await conn.execute("""
                update public.shopify_actions set provider_id = %s, completed_at = now()
                where store_id = %s and token = %s
            """, (provider_id, store_id, token))
