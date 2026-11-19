import logging
from time import time
from typing import Any

import httpx
from google import genai
from google.genai import types
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from psycopg_pool import AsyncConnectionPool

from .ai_orchestrator import Orchestrator
from .config import BULK_QUEUE, DAY, HIGH_QUEUE, settings, stores
from .repository import Repository
from .schemas import BulkCODJob, WhatsAppMessage
from .telemetry import configure, hop, observe, response_clock

PROPAGATOR = TraceContextTextMapPropagator()
LOG = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    # ARQ's default logs can include raw job arguments and provider exceptions.
    logging.getLogger("arq.worker").disabled = True
    configure("bhashacart-" + ctx["queue"])
    stores()
    cfg = settings()
    dsn = cfg.supabase_postgres_dsn.get_secret_value()
    key = cfg.gemini_api_key.get_secret_value()
    if not dsn or not key:
        raise RuntimeError("SUPABASE_POSTGRES_DSN and GEMINI_API_KEY are required for workers")

    pool = AsyncConnectionPool(conninfo=dsn, min_size=1, max_size=8, timeout=3, open=False)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(5, connect=2), follow_redirects=False,
        limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
    )
    ai = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=5000)).aio
    try:
        await pool.open(wait=True, timeout=10)
    except BaseException:
        await pool.close()
        await http.aclose()
        await ai.aclose()
        raise
    ctx.update(pool=pool, http=http, ai=ai, pipeline=Orchestrator(http, ctx["redis"], Repository(pool), ai))
    LOG.warning("Worker ready for %s", ctx["queue"])


async def startup_live(ctx: dict[str, Any]) -> None:
    ctx["queue"] = HIGH_QUEUE
    await startup(ctx)


async def startup_bulk(ctx: dict[str, Any]) -> None:
    ctx["queue"] = BULK_QUEUE
    await startup(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["ai"].aclose()
    await ctx["pool"].close()


async def run_job(
    ctx: dict[str, Any],
    phone_id: str,
    payload: str,
    received_at: float,
    carrier: dict[str, str],
    *,
    bulk: bool,
) -> None:
    name = "bulk" if bulk else "live"
    store = stores().get(phone_id)
    if not store:
        LOG.warning("Job skipped because its store is no longer configured")
        return
    parent = PROPAGATOR.extract(carrier)
    with trace.get_tracer(__name__).start_as_current_span(
        f"worker.{name}", context=parent, record_exception=False, set_status_on_exception=False
    ) as span:
        observe(f"{name}_queue_age", (time() - received_at) * 1000)
        try:
            with hop(f"{name}_worker_total"):
                pipeline: Orchestrator = ctx["pipeline"]
                if bulk:
                    await pipeline.bulk(phone_id, store, BulkCODJob.model_validate_json(payload))
                else:
                    with response_clock(received_at):
                        await pipeline.live(phone_id, store, WhatsAppMessage.model_validate_json(payload))
        except Exception as exc:
            span.set_status(trace.StatusCode.ERROR)
            LOG.warning("%s job failed (%s)", name, type(exc).__name__)
            # Keep provider response bodies and customer data out of job results.
            raise RuntimeError(f"{name} job failed: {type(exc).__name__}") from None
        observe(f"{name}_accepted_to_complete", (time() - received_at) * 1000)


async def process_live(
    ctx: dict[str, Any], phone_id: str, payload: str, received_at: float, carrier: dict[str, str]
) -> None:
    await run_job(ctx, phone_id, payload, received_at, carrier, bulk=False)


async def process_bulk(
    ctx: dict[str, Any], phone_id: str, payload: str, received_at: float, carrier: dict[str, str]
) -> None:
    await run_job(ctx, phone_id, payload, received_at, carrier, bulk=True)


class LiveWorkerSettings:
    functions = [process_live]
    on_startup = startup_live
    on_shutdown = shutdown
    queue_name = HIGH_QUEUE
    redis_settings = settings().redis_settings()
    max_jobs = 32
    poll_delay = 0.025
    job_timeout = 30
    max_tries = 2
    keep_result = DAY
    log_results = False
    job_completion_wait = 25
    health_check_interval = 10


class BulkWorkerSettings:
    functions = [process_bulk]
    on_startup = startup_bulk
    on_shutdown = shutdown
    queue_name = BULK_QUEUE
    redis_settings = settings().redis_settings()
    max_jobs = 2
    poll_delay = 0.25
    job_timeout = 20
    max_tries = 1
    keep_result = DAY
    log_results = False
    job_completion_wait = 15
    health_check_interval = 10
