import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import time
from typing import Annotated

from arq.connections import ArqRedis, create_pool
from fastapi import FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import ValidationError
from redis.exceptions import RedisError

from .config import BULK_QUEUE, DAY, HIGH_QUEUE, Digits, settings, stores
from .schemas import BulkCODJob, WhatsAppWebhook
from .telemetry import configure, hop

MAX_WEBHOOK_BYTES = 512 * 1024
PROPAGATOR = TraceContextTextMapPropagator()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stores()
    app.state.redis = await create_pool(settings().redis_settings())
    try:
        yield
    finally:
        await app.state.redis.aclose()


configure("bhashacart-api")
app = FastAPI(title="BhashaCart AI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# The GET verification URL contains a secret query parameter. POST uses its own span.
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,/webhooks/whatsapp")


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def ready(request: Request) -> dict[str, str]:
    try:
        await request.app.state.redis.ping()
    except RedisError:
        raise HTTPException(status_code=503, detail="queue unavailable") from None
    return {"status": "ready"}


def valid_secret(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


@app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def verify(
    mode: Annotated[str, Query(alias="hub.mode")],
    token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge", pattern=r"^[0-9]{1,128}$")],
) -> str:
    if mode != "subscribe" or not valid_secret(token, settings().whatsapp_verify_token.get_secret_value()):
        raise HTTPException(status_code=403, detail="verification refused")
    return challenge


async def enqueue(
    redis: ArqRedis,
    queue: str,
    job: str,
    key: str,
    phone_id: str,
    payload: str,
    received_at: float,
) -> None:
    if await redis.exists(key):
        return
    trace_headers: dict[str, str] = {}
    PROPAGATOR.inject(trace_headers)
    job_id = hashlib.sha256(key.encode()).hexdigest()
    # ARQ atomically reserves its deterministic job ID and adds the queue entry.
    # Write the longer-lived marker only after a job exists so an enqueue failure
    # can be retried without leaving a marker for work that was never queued.
    await redis.enqueue_job(
        job, phone_id, payload, received_at, trace_headers,
        _job_id=job_id, _queue_name=queue, _expires=DAY,
    )
    await redis.set(key, "1", nx=True, ex=DAY)


@app.post("/webhooks/whatsapp")
async def receive(
    request: Request,
    signature: Annotated[str, Header(alias="X-Hub-Signature-256")] = "",
) -> Response:
    received_at = time()
    with hop("webhook_queue"):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="webhook too large")
            raw.extend(chunk)
        secret = settings().whatsapp_app_secret.get_secret_value()
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not secret or not valid_secret(signature, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        try:
            webhook = WhatsAppWebhook.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from None

        redis: ArqRedis = request.app.state.redis
        try:
            for entry in webhook.entry:
                for change in entry.changes:
                    metadata = change.value.metadata
                    if change.field != "messages" or not metadata or metadata.phone_number_id not in stores():
                        continue
                    phone_id = metadata.phone_number_id
                    for message in change.value.messages:
                        if message.type in {"text", "audio", "interactive", "button"}:
                            await enqueue(
                                redis, HIGH_QUEUE, "process_live",
                                f"wa:dedup:{phone_id}:{message.id}", phone_id,
                                message.model_dump_json(by_alias=True), received_at,
                            )
        except RedisError:
            raise HTTPException(status_code=503, detail="queue unavailable") from None
    return Response(status_code=200)


@app.post("/internal/cod/{phone_id}", status_code=202)
async def schedule_cod(
    request: Request,
    phone_id: Annotated[Digits, Path()],
    job: BulkCODJob,
    authorization: Annotated[str, Header()] = "",
) -> dict[str, str]:
    store = stores().get(phone_id)
    token = store.bulk_api_token.get_secret_value() if store else ""
    if not store or not token or not valid_secret(authorization, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="verification refused")
    if not store.cod_template:
        raise HTTPException(status_code=409, detail="COD template not configured")
    order = job.order_name.lstrip("#").casefold()
    try:
        await enqueue(
            request.app.state.redis, BULK_QUEUE, "process_bulk",
            f"cod:dedup:{phone_id}:{job.campaign_id}:{order}", phone_id, job.model_dump_json(), time(),
        )
    except RedisError:
        raise HTTPException(status_code=503, detail="queue unavailable") from None
    return {"status": "accepted"}
