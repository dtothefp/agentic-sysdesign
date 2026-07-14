"""Module 7: the sysdesign chat agent.

Public surface is small on purpose. `run_agent` is the whole loop; `TOOL_SCHEMAS` and
`run_tool` are the manually registered tool layer. The CLI (`python -m agent`) and the SSE
server (`agent.server:app`) are two thin transports over the same `run_agent` generator.
"""

from .loop import run_agent
from .tools import TOOL_SCHEMAS, Tool, run_tool

__all__ = ["run_agent", "Tool", "TOOL_SCHEMAS", "run_tool"]
