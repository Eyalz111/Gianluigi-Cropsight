"""Orchestration spine — the single seam for inbound/outbound comms.

See V2.5_STRATEGY.md §6 (the comms/voice "human-assistant" layer). The spine
imports its external dependencies lazily, so importing this package never pulls
in the Telegram bot / agent at module-load time.
"""
from services.orchestrator.events import Channel, InboundEvent, Modality
from services.orchestrator.spine import CommsSpine, comms_spine

__all__ = ["Channel", "Modality", "InboundEvent", "CommsSpine", "comms_spine"]
