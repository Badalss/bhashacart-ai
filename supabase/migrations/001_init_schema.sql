begin;

create schema if not exists extensions;
create extension if not exists vector with schema extensions;

create role bhashacart_app nologin nosuperuser nobypassrls;
grant usage on schema public, extensions to bhashacart_app;

create table public.stores (
    store_id uuid primary key,
    display_name text not null
);

create table public.faqs (
    store_id uuid not null references public.stores,
    faq_id uuid not null default gen_random_uuid(),
    locale text not null check (locale in ('hi-IN', 'ta-IN', 'bn-IN', 'en-IN')),
    topic text not null check (topic in ('shipping', 'returns', 'cod', 'payments', 'general')),
    question text not null check (char_length(question) between 1 and 4096),
    answer text not null check (char_length(answer) between 1 and 1024),
    embedding_model text not null,
    embedding extensions.vector(768) not null,
    primary key (store_id, faq_id)
);
create index faq_scope_idx on public.faqs (store_id, locale, topic, embedding_model);

create table public.handoffs (
    store_id uuid not null references public.stores,
    event_id text not null,
    sender text not null,
    reason text not null,
    created_at timestamptz not null default now(),
    resolved_at timestamptz,
    primary key (store_id, event_id)
);

create table public.shopify_actions (
    store_id uuid not null references public.stores,
    token text not null,
    kind text not null check (kind in ('confirm_cod', 'initiate_return')),
    order_id text not null,
    line_id text,
    provider_id text,
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    primary key (store_id, token),
    unique nulls not distinct (store_id, kind, order_id, line_id),
    check ((kind = 'initiate_return') = (line_id is not null))
);

do $policies$
declare relation text;
begin
    foreach relation in array array['stores', 'faqs', 'handoffs', 'shopify_actions'] loop
        execute format('alter table public.%I enable row level security', relation);
        execute format('alter table public.%I force row level security', relation);
        execute format('revoke all on public.%I from public, anon, authenticated, service_role', relation);
        execute format(
            'create policy store_scope on public.%I for all to bhashacart_app
             using (store_id = nullif(current_setting(''app.store_id'', true), '''')::uuid)
             with check (store_id = nullif(current_setting(''app.store_id'', true), '''')::uuid)',
            relation
        );
    end loop;
end
$policies$;

grant select on public.faqs to bhashacart_app;
grant insert on public.handoffs to bhashacart_app;
grant select, insert on public.shopify_actions to bhashacart_app;
grant update (provider_id, completed_at) on public.shopify_actions to bhashacart_app;

commit;
