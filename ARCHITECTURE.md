# BhashaCart AI architecture

BhashaCart AI separates webhook acknowledgment from the provider calls that resolve a conversation. The webhook authorizes the source and maps each incoming Meta phone number ID to a configured store. The worker carries that trusted mapping into Shopify, Valkey cache keys, and Postgres transactions.

## Code-switching and voice notes

- **Voice intake.** The live worker downloads bounded audio files using the store's Meta credentials. Sarvam runs in `codemix` mode so the transcript can preserve embedded English and regional-language text. The Bhashini adapter converts input to 16 kHz mono WAV and selects a configured source-language service using the store locale. Evaluate each Bhashini service with representative mixed-language recordings before enabling it.
- **Text and transliteration.** The same routing prompt handles native script, Romanized Hinglish and Tanglish, and common English commerce terms. It asks Gemini to determine the response locale and copy the order name exactly. The server checks the original text or transcript for a unique matching order identifier. If pronunciation or transcription changes the digits, the customer can re-enter the identifier as text.
- **Confidence.** A numeric ASR score below `ASR_MIN_CONFIDENCE` triggers a clarification message and an interactive support option. A provider may omit its score. With unknown confidence, tracking requires a unique, verbatim order identifier and verified ownership. COD and return requests require the customer to re-enter the information as text. Ambiguous Gemini output follows the same clarification path.
- **Replies.** Localized templates cover clarification, support, tracking, and confirmation. FAQ replies use merchant-authored language variants. Regional TTS streams audio from Sarvam to the worker, which assembles the clip for the Meta media endpoint. The optional Bhashini TTS adapter returns a complete clip and reports batch synthesis latency. Validate vernacular phrasing and pronunciation with native speakers for each merchant's vocabulary.

## Guardrails for answers and actions

1. **Constrained model output.** Gemini emits a single structured JSON intent. The server validates the full result with strict Pydantic unions for `CheckOrderIntent`, `ConfirmCODIntent`, `InitiateReturnIntent`, `EscalateToHumanIntent`, or `FAQIntent`. The worker selects from fixed application handlers. GraphQL and SQL are static statements with validated inputs and bound variables.
2. **Verified ownership.** Look up orders by an exact identifier within the configured Shopify store. Compare the WhatsApp sender against the order or shipping phone available from Shopify. Missing permission, a nonmatching phone, or multiple candidates blocks automatic disclosure. Tracking replies are assembled from verified Shopify fields.
3. **Explicit customer consent.** COD and return flows issue an opaque, expiring action button bound to the sender and store. The worker checks the binding and rechecks order ownership on click. COD verifies the configured payment gateway and payment status. Return initiation supports an unambiguous single eligible quantity-one line and submits a Shopify return request for merchant review. More complex return selections go to support. [Shopify return requests](https://shopify.dev/docs/api/admin-graphql/2026-07/mutations/returnRequest)
4. **Grounded policy replies.** The FAQ route embeds an eligible question and searches merchant-approved material scoped to the store, locale, topic, and embedding model. Warm small buckets use cosine similarity over the complete approved question-vector set in Valkey. Cold or larger buckets use pgvector. A minimum score and separation from the runner-up govern retrieval. Weak or conflicting matches create a support handoff. The cache stores approved merchant entries for five minutes.
5. **Durable action ledger.** The worker commits a Postgres action claim before invoking a Shopify mutation. The unique business key covers the tenant, action kind, order, and return line where applicable. Confirmed successes keep the Shopify ID. Uncertain outcomes route to human reconciliation. The worker retries definitive throttling responses with bounded backoff and treats transport-level mutation failures as uncertain.

Review unresolved `public.handoffs` rows through a trusted merchant support process. If Shopify accepted an action but its response or the ledger update was lost, check Shopify before relaying the result or reconciling the action record. An operator must adjudicate repeated return requests for the same fulfillment line.

## Diwali queue isolation

`high_priority_chat` and `bulk_cod_verification` have dedicated ARQ worker processes and concurrency budgets. All incoming WhatsApp customer messages and confirmation clicks go to the high-priority queue. Only authenticated merchant campaign submissions enter the bulk queue. Both workers still share Valkey and may call Shopify for the same store. Scale them independently, control producer throughput, and monitor Shopify throttling by workload at peak volume.

Ingress scopes WhatsApp message IDs to the registered receiving number and uses deterministic ARQ job IDs plus Valkey markers created with `SET NX EX 86400`. A queued job can be recovered on webhook redelivery after an interrupted acknowledgment. The durable Shopify ledger protects write actions even after short-lived queue markers expire. Outbound WhatsApp responses are separate provider calls with their own delivery outcome.

The Compose Valkey service persists its queue data. In production, use privately networked Valkey with persistence and failover, and route bulk submits through a private or authenticated ingress. Watch queue depth, age of the oldest job, provider throttling, handoffs, and unresolved action claims. Pause or slow campaign producers if Shopify or Valkey approaches capacity. Separate per-store submission and rate budgets are valuable for tenants sharing these services.

## Sub-800 ms optimization and measurement

Deploy the API, workers, and Valkey in `ap-south-1` and choose the closest available Supabase region and provider endpoints. Measure network round-trip times to Meta, Sarvam or Bhashini, Gemini, and Shopify before assigning production SLOs. Reuse worker HTTP connections and Postgres pools, keep provider clients warm, and give live jobs dedicated processing capacity.

Gemini uses a short deterministic classification prompt with a capped structured response. Stream Gemini to observe TTFT, then validate the complete schema before running a tool. FAQ caching and grounded replies avoid a separate model generation pass. Tracking typically needs one Shopify request. Return selection, mutation confirmation, and campaign delivery have different call counts and should be measured separately.

The `bhashacart.hop.duration` histogram records milliseconds with bounded hop and outcome dimensions. Track webhook acceptance, queue dwell, ASR, Gemini TTFT and total, Shopify time, TTS first chunk and total, and Meta media operations. Report P50, P95, sample count and errors per path. Evaluate live text, live voice, FAQ cache hit/miss, Shopify read, and bulk COD separately, then drive concurrency and coverage with recorded load profiles. Keep message text, transcripts, raw media, phone numbers, and credentials out of telemetry.

The first audio byte marks streaming synthesis readiness. WhatsApp requires the complete response audio for upload and delivery. Measure `live_ingress_to_response_ready` by response kind and use `live_accepted_to_complete` to assess the additional cost of WhatsApp sends and remaining work. Use WhatsApp delivery status events externally when measuring handset delivery. The 800 ms number in the README is a design budget for the stated warm-path hops.

## Tenant isolation and data handling

Each provider lookup uses the trusted store configured for the receiving Meta number. FAQ and action keys are store-scoped in Valkey. The database sets `app.store_id` transaction-locally using a bound trusted value at connection checkout, and every tenant table has forced RLS. Use the constrained `bhashacart_runtime` login in workers and restrict it to the backend network. Provisioning and support administration use separate credentials and access controls.

Queue payloads, handoff records, Meta audio, and provider requests can contain personal data. Configure tenant retention and deletion policies, restrict database and Valkey access, and review the applicable provider data-handling terms before onboarding a merchant. Merchant teams should monitor responses in Hindi, Tamil, Bengali, Hinglish and Tanglish and tune the clarification threshold with attributed, consented samples.
