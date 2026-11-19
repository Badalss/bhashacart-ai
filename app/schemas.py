from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, field_validator, model_validator

from .config import Digits, Locale

OrderName = Annotated[str, StringConstraints(pattern=r"^#?[A-Za-z0-9][A-Za-z0-9-]{0,31}$")]
EventId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._=:+/-]{1,255}$")]
OrderGid = Annotated[str, StringConstraints(pattern=r"^gid://shopify/Order/[0-9]+$")]
LineGid = Annotated[str, StringConstraints(pattern=r"^gid://shopify/FulfillmentLineItem/[0-9]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class WhatsAppModel(BaseModel):
    # Meta evolves the envelope independently. Validate consumed fields strictly.
    model_config = ConfigDict(strict=True, extra="ignore")


class TextBody(WhatsAppModel):
    body: str = Field(min_length=1, max_length=4096)


class AudioBody(WhatsAppModel):
    id: Digits


class Button(WhatsAppModel):
    id: EventId


class InteractiveBody(WhatsAppModel):
    type: str
    button_reply: Button | None = None


class TemplateButton(WhatsAppModel):
    payload: EventId


class WhatsAppMessage(WhatsAppModel):
    id: EventId
    sender: Digits = Field(alias="from")
    type: str
    text: TextBody | None = None
    audio: AudioBody | None = None
    interactive: InteractiveBody | None = None
    button: TemplateButton | None = None

    @model_validator(mode="after")
    def check_body(self) -> "WhatsAppMessage":
        if self.type in {"text", "audio", "interactive", "button"} and getattr(self, self.type) is None:
            raise ValueError("message body missing")
        return self


class Metadata(WhatsAppModel):
    phone_number_id: Digits


class WhatsAppValue(WhatsAppModel):
    metadata: Metadata | None = None
    messages: list[WhatsAppMessage] = Field(default_factory=list)


class WhatsAppChange(WhatsAppModel):
    field: str
    value: WhatsAppValue


class WhatsAppEntry(WhatsAppModel):
    changes: list[WhatsAppChange]


class WhatsAppWebhook(WhatsAppModel):
    object: Literal["whatsapp_business_account"]
    entry: list[WhatsAppEntry]


class Intent(StrictModel):
    locale: Locale
    ambiguous: bool


class CheckOrderIntent(Intent):
    kind: Literal["check_order"]
    order_name: OrderName


class ConfirmCODIntent(Intent):
    kind: Literal["confirm_cod"]
    order_name: OrderName


class InitiateReturnIntent(Intent):
    kind: Literal["initiate_return"]
    order_name: OrderName


class EscalateToHumanIntent(Intent):
    kind: Literal["escalate_to_human"]


class FAQIntent(Intent):
    kind: Literal["faq"]
    topic: Literal["shipping", "returns", "cod", "payments", "general"]


IntentAdapter = TypeAdapter(
    Annotated[
        CheckOrderIntent | ConfirmCODIntent | InitiateReturnIntent | EscalateToHumanIntent | FAQIntent,
        Field(discriminator="kind"),
    ]
)


class ReturnParameters(StrictModel):
    order_id: OrderGid
    line_id: LineGid
    quantity: Literal[1] = 1


class PendingAction(StrictModel):
    sender: Digits
    kind: Literal["confirm_cod", "initiate_return"]
    order_id: OrderGid
    locale: Locale
    line_id: LineGid | None = None

    @model_validator(mode="after")
    def check_line(self) -> "PendingAction":
        if (self.kind == "initiate_return") != (self.line_id is not None):
            raise ValueError("return line must accompany return actions only")
        return self


class BulkCODJob(StrictModel):
    campaign_id: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    order_name: OrderName
    recipient: Digits
    opted_in: bool

    @field_validator("opted_in")
    @classmethod
    def require_opt_in(cls, value: bool) -> bool:
        if not value:
            raise ValueError("recipient opt-in is required")
        return value
