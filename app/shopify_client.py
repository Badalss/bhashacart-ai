import asyncio
import random
import re
from typing import Any

import httpx

from .config import Store, settings
from .schemas import OrderName, ReturnParameters
from .telemetry import hop

ORDER_FIELDS = """
    id name phone shippingAddress { phone } cancelledAt displayFinancialStatus
    displayFulfillmentStatus paymentGatewayNames tags
    fulfillments(first: 5) { trackingInfo(first: 3) { company number } }
"""


class ShopifyError(Exception):
    pass


class Shopify:
    def __init__(self, http: httpx.AsyncClient, store: Store):
        self.http = http
        self.store = store
        self.url = f"https://{store.shop_domain}/admin/api/{settings().shopify_api_version}/graphql.json"

    async def graphql(
        self, query: str, variables: dict[str, Any], *, mutation: bool = False
    ) -> dict[str, Any]:
        with hop("shopify_api"):
            for attempt in range(3):
                response = await self.http.post(
                    self.url, json={"query": query, "variables": variables},
                    headers={"X-Shopify-Access-Token": self.store.shopify_token.get_secret_value()},
                )
                payload = response.json() if response.is_success else {}
                errors = payload.get("errors", [])
                throttled = response.status_code == 429 or (
                    bool(errors) and all(error.get("extensions", {}).get("code") == "THROTTLED"
                                         for error in errors)
                )
                if not throttled:
                    response.raise_for_status()
                    if errors or not isinstance(payload.get("data"), dict):
                        raise ShopifyError("Shopify operation failed")
                    return payload["data"]
                if attempt == 2:
                    raise ShopifyError("Shopify throttle budget exhausted")
                retry_after = response.headers.get("Retry-After", "")
                cost = payload.get("extensions", {}).get("cost", {})
                budget = cost.get("throttleStatus", {})
                deficit = cost.get("requestedQueryCost", 0) - budget.get("currentlyAvailable", 0)
                restore = budget.get("restoreRate", 0)
                wait = max(0.15 * 2**attempt, deficit / restore if restore else 0)
                if retry_after.replace(".", "", 1).isdigit():
                    wait = max(wait, float(retry_after))
                if wait > (1 if mutation else 2):
                    raise ShopifyError("Shopify retry delay exceeds request budget")
                await asyncio.sleep(wait + random.uniform(0, 0.05))
        raise AssertionError("unreachable")

    @staticmethod
    def owns(order: dict[str, Any], sender: str) -> bool:
        phones = (order.get("phone"), (order.get("shippingAddress") or {}).get("phone"))
        for phone in phones:
            canonical = re.sub(r"[^0-9]", "", phone or "")
            if len(canonical) == 10:
                canonical = "91" + canonical
            if canonical and canonical == sender:
                return True
        return False

    async def find_order(self, name: OrderName, sender: str) -> dict[str, Any] | None:
        # name is already constrained by Pydantic before building Shopify search syntax.
        names = (name,) if name.startswith("#") else (name, f"#{name}")
        exact = {candidate.casefold() for candidate in names}
        data = await self.graphql(
            "query ($query: String!) { orders(first: 10, query: $query) { "
            "pageInfo { hasNextPage } nodes { " + ORDER_FIELDS + " } } }",
            {"query": " OR ".join(f'name:"{candidate}"' for candidate in names)},
        )
        result = data["orders"]
        matches = [order for order in result["nodes"] if order["name"].casefold() in exact]
        if len(matches) == 1 and not result["pageInfo"]["hasNextPage"] and self.owns(matches[0], sender):
            return matches[0]
        return None

    async def get_order(self, order_id: str, sender: str) -> dict[str, Any] | None:
        result = await self.graphql(
            "query ($id: ID!) { order(id: $id) { " + ORDER_FIELDS + " } }", {"id": order_id}
        )
        order = result["order"]
        return order if order and self.owns(order, sender) else None

    def eligible_cod(self, order: dict[str, Any]) -> bool:
        return (not order["cancelledAt"]
                and order["displayFinancialStatus"] in {"PENDING", "AUTHORIZED"}
                and bool(set(order["paymentGatewayNames"]) & set(self.store.cod_gateways))
                and "bhashacart-cod-confirmed" not in order["tags"])

    async def sole_return_line(self, order_id: str) -> tuple[str, str] | None:
        data = await self.graphql("""
            query ($id: ID!) {
              returnableFulfillments(orderId: $id, first: 2) {
                pageInfo { hasNextPage }
                nodes {
                  returnableFulfillmentLineItems(first: 2) {
                    pageInfo { hasNextPage }
                    nodes {
                      quantity
                      fulfillmentLineItem { id lineItem { name } }
                    }
                  }
                }
              }
            }
        """, {"id": order_id})
        result = data["returnableFulfillments"]
        if result["pageInfo"]["hasNextPage"] or len(result["nodes"]) != 1:
            return None
        items = result["nodes"][0]["returnableFulfillmentLineItems"]
        if items["pageInfo"]["hasNextPage"] or len(items["nodes"]) != 1:
            return None
        line = items["nodes"][0]
        if line["quantity"] != 1:
            return None
        item = line["fulfillmentLineItem"]
        return item["id"], item["lineItem"]["name"]

    async def confirm_cod(self, order_id: str) -> str:
        data = await self.graphql("""
            mutation ($id: ID!) {
              tagsAdd(id: $id, tags: ["bhashacart-cod-confirmed"]) {
                node { id }
                userErrors { message }
              }
            }
        """, {"id": order_id}, mutation=True)
        result = data["tagsAdd"]
        if not result or result["userErrors"] or not result["node"]:
            raise ShopifyError("COD tag change rejected")
        return result["node"]["id"]

    async def request_return(self, params: ReturnParameters) -> str:
        data = await self.graphql("""
            mutation ($input: ReturnRequestInput!) {
              returnRequest(input: $input) {
                return { id }
                userErrors { message }
              }
            }
        """, {"input": {"orderId": params.order_id, "returnLineItems": [
            {"fulfillmentLineItemId": params.line_id, "quantity": params.quantity}
        ]}}, mutation=True)
        result = data["returnRequest"]
        if not result or result["userErrors"] or not result["return"]:
            raise ShopifyError("return request rejected")
        return result["return"]["id"]
