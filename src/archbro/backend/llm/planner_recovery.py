from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_legacy_explicit_429_checkpoint(checkpoint: Mapping[str, Any]) -> bool:
    """Identify the narrow v4 checkpoint shape that proves a rejected call.

    Older planner versions persisted Google ``ClientError`` 429 responses as
    ``UNKNOWN``/``IN_FLIGHT`` before structured provider-response telemetry
    existed. This exact signature is safe to resume because Vertex explicitly
    rejected the request; other message-only or transport failures remain
    ambiguous and must keep the paid-call authorization boundary.
    """

    status = str(checkpoint.get("status") or "UNKNOWN").upper()
    delivery_stage = str(
        checkpoint.get("delivery_stage") or "PREPARED"
    ).upper()
    provider = checkpoint.get("provider")
    validation = checkpoint.get("validation")
    validation_message = (
        str(validation.get("message") or "")
        if isinstance(validation, Mapping)
        else ""
    )
    return bool(
        status == "UNKNOWN"
        and delivery_stage == "IN_FLIGHT"
        and isinstance(provider, Mapping)
        and provider.get("error_type") == "ClientError"
        and "429" in validation_message
        and "RESOURCE_EXHAUSTED" in validation_message.upper()
    )


def has_ambiguous_paid_call_outcome(checkpoint: Mapping[str, Any]) -> bool:
    """Return whether a checkpoint may represent an accepted upstream call."""

    if is_legacy_explicit_429_checkpoint(checkpoint):
        return False
    status = str(checkpoint.get("status") or "UNKNOWN").upper()
    delivery_stage = str(
        checkpoint.get("delivery_stage") or "PREPARED"
    ).upper()
    if status in {
        "RETRYABLE",
        "REPROCESSABLE",
        "REPAIR_REQUIRED",
        "COMPLETED",
        "FAILED",
    }:
        # RETRYABLE may retain the historical IN_FLIGHT delivery stage after
        # an explicit AUTHORIZE_NEW_ATTEMPT recovery. The status is the fenced
        # human decision and must take precedence over the old stage marker.
        return False
    return status == "UNKNOWN" or delivery_stage == "IN_FLIGHT"
