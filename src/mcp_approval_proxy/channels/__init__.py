"""
Optional notification/fallback channels.

These are NOT used by the main proxy (which uses MCP-native elicitation).
They are kept for integration scenarios where you want to forward approvals
to an external system (webhook, WhatsApp, etc.) as a post-approval notification.
"""

from .webhook import WebhookChannel
from .whatsapp import WhatsAppChannel

__all__ = [
    "WebhookChannel",
    "WhatsAppChannel",
]
