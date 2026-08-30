"""Conversation-private attachments.

Keep this package initializer dependency-free: persistence imports the tools
package while attachment storage itself uses persistence.
"""

from .contracts import ATTACHMENT_TOOL_NAMES

__all__ = ["ATTACHMENT_TOOL_NAMES"]
