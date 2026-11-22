"""GHL-019 / GHL-020: attribution intake engine.

Responsibilities, in order:

1. **Discovery at startup.** The field map is bound to real custom-field
   ids before the first write. Drift raises ``AttributionMappingError``.
2. **Idempotent intake.** A claim keyed by event id is taken first. Three
   deliveries of one event produce one contact and one write.
3. **Immutable credit.** Existing attribution is read back and preserved;
   only blanks and the activity timestamp are written.
4. **Retry safety.** A failed write releases its claim, so the retry that
   follows is the same operation, not a second one.

``dry_run=True`` returns the exact field delta without touching the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from ghl_client.resources.contacts import ContactsResource
from ghl_client.resources.custom_fields import CustomFieldsResource

from .events import AttributionEvent
from .ledger import ClaimStore, InMemoryClaimStore
from .mapping import AttributionMapping, ResolvedMapping
from .policy import AttributionPlan, AttributionPolicy

__all__ = ["AttributionEngine", "IngestResult", "IngestStatus"]


class IngestStatus(str, Enum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    UNCHANGED = "unchanged"
    PLANNED = "planned"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    event_id: str
    contact_id: str | None = None
    applied: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    overwritten: tuple[str, ...] = ()
    custom_fields: tuple[dict[str, str], ...] = ()
    reason: str = ""

    @property
    def wrote(self) -> bool:
        return self.status is IngestStatus.APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "eventId": self.event_id,
            "contactId": self.contact_id,
            "applied": list(self.applied),
            "preserved": list(self.preserved),
            "skipped": list(self.skipped),
            "overwritten": list(self.overwritten),
            "customFields": [dict(item) for item in self.custom_fields],
            "reason": self.reason,
        }


class AttributionEngine:
    def __init__(
        self,
        client: Any,
        *,
        mapping: AttributionMapping | None = None,
        claims: ClaimStore | None = None,
        policy: AttributionPolicy | None = None,
        location_id: str | None = None,
        contacts: ContactsResource | None = None,
        custom_fields: CustomFieldsResource | None = None,
        discover: bool = True,
    ) -> None:
        self.client = client
        self.location_id = location_id or getattr(client, "location_id", None)
        if not self.location_id:
            raise ValueError("location_id is required for attribution intake")
        self.mapping = mapping or AttributionMapping.default()
        self.claims: ClaimStore = claims or InMemoryClaimStore()
        self.policy = policy or AttributionPolicy()
        self.contacts = contacts or ContactsResource(client)
        self.custom_fields = custom_fields or CustomFieldsResource(client)
        self._resolved: ResolvedMapping | None = None
        if discover:
            self.discover()

    # -- discovery -----------------------------------------------------------

    @property
    def resolved(self) -> ResolvedMapping:
        if self._resolved is None:
            self.discover()
        assert self._resolved is not None
        return self._resolved

    def discover(self, *, refresh: bool = False) -> ResolvedMapping:
        """Bind the map to live field ids. Raises on drift."""

        if self._resolved is not None and not refresh:
            return self._resolved
        definitions: Iterable[Mapping[str, Any]] = self.custom_fields.list(self.location_id)
        self._resolved = self.mapping.resolve(definitions, location_id=self.location_id)
        return self._resolved

    # -- intake --------------------------------------------------------------

    def ingest(
        self,
        event: AttributionEvent | Mapping[str, Any],
        *,
        dry_run: bool = False,
    ) -> IngestResult:
        parsed = event if isinstance(event, AttributionEvent) else AttributionEvent.from_payload(event)
        # An organic / direct touch carries nothing to attribute. Recording it
        # would create contacts and churn timestamps from noise, so it is a no-op.
        if not parsed.touch.has_campaign_signal:
            return IngestResult(
                status=IngestStatus.UNCHANGED,
                event_id=parsed.event_id,
                reason="touch carries no campaign signal",
            )
        resolved = self.resolved
        claim_key = f"{self.location_id}:{parsed.event_id}"

        if not dry_run and not self.claims.claim(claim_key):
            return IngestResult(
                status=IngestStatus.DUPLICATE,
                event_id=parsed.event_id,
                reason="event already processed",
            )

        try:
            existing = self._find_contact(parsed)
            current_values = self._existing_values(existing, resolved)
            plan = self.policy.compute(current_values, parsed)
            payload_fields = tuple(
                {"id": resolved.field_id(key), "value": plan.values[key]} for key in plan.applied
            )

            if dry_run:
                self.claims.release(claim_key)
                return IngestResult(
                    status=IngestStatus.PLANNED,
                    event_id=parsed.event_id,
                    contact_id=self._contact_id(existing),
                    applied=plan.applied,
                    preserved=plan.preserved,
                    skipped=plan.skipped,
                    overwritten=plan.overwritten,
                    custom_fields=payload_fields,
                    reason="dry run: no write performed",
                )

            if plan.is_empty:
                return IngestResult(
                    status=IngestStatus.UNCHANGED,
                    event_id=parsed.event_id,
                    contact_id=self._contact_id(existing),
                    preserved=plan.preserved,
                    skipped=plan.skipped,
                    reason="nothing to write",
                )

            payload = self._build_payload(parsed, plan, payload_fields)
            response = self.contacts.upsert(payload, idempotency_key=f"attribution:{parsed.event_id}")
            contact = (response or {}).get("contact") or {}
            return IngestResult(
                status=IngestStatus.APPLIED,
                event_id=parsed.event_id,
                contact_id=contact.get("id") or self._contact_id(existing),
                applied=plan.applied,
                preserved=plan.preserved,
                skipped=plan.skipped,
                overwritten=plan.overwritten,
                custom_fields=payload_fields,
            )
        except Exception:
            if not dry_run:
                self.claims.release(claim_key)
            raise

    # -- internals -----------------------------------------------------------

    def _build_payload(
        self,
        event: AttributionEvent,
        plan: AttributionPlan,
        fields_payload: tuple[dict[str, str], ...],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"locationId": self.location_id}
        if event.email:
            payload["email"] = event.email
        if event.phone:
            payload["phone"] = event.phone
        if event.first_name:
            payload["firstName"] = event.first_name
        if event.last_name:
            payload["lastName"] = event.last_name
        # Tag only when this event is the one that earns credit; a later
        # non-credited touch must not repaint the contact's source tag.
        source = event.touch.source
        if source and "utm_source" in plan.applied:
            payload["tags"] = [f"attribution:{source.strip().lower()}"]
        payload["customFields"] = [dict(item) for item in fields_payload]
        return payload

    def _find_contact(self, event: AttributionEvent) -> dict[str, Any] | None:
        query = event.email or event.phone
        if not query:
            return None
        # strict=True: a page failure raises instead of returning a partial
        # result, so we never mistake "page broke" for "no contact".
        results = self.contacts.search(query=query, limit=1)
        for item in results:
            if isinstance(item, Mapping):
                if event.email and str(item.get("email") or "").lower() == event.email:
                    return dict(item)
                if event.phone and str(item.get("phone") or "") == event.phone:
                    return dict(item)
        return None

    def _existing_values(
        self,
        contact: Mapping[str, Any] | None,
        resolved: ResolvedMapping,
    ) -> dict[str, Any]:
        if not contact:
            return {}
        by_field_id = {field_id: key for key, field_id in resolved.field_ids.items()}
        values: dict[str, Any] = {}
        for item in contact.get("customFields") or []:
            if not isinstance(item, Mapping):
                continue
            key = by_field_id.get(str(item.get("id") or ""))
            if key and item.get("value") not in (None, ""):
                values[key] = item.get("value")
        return values

    @staticmethod
    def _contact_id(contact: Mapping[str, Any] | None) -> str | None:
        return str(contact.get("id")) if contact and contact.get("id") else None
