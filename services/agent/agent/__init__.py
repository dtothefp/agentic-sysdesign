"""Module 7: the sysdesign chat agent.

Public surface is small on purpose. `run_agent` is the whole loop; `Tool`/`Toolbox`/
`default_toolbox` are the tool layer. The CLI (`python -m agent`) and the SSE server
(`agent.server:app`) are two thin transports over the same `run_agent` generator.
"""

from .loop import run_agent
from .tools import Tool, Toolbox, default_toolbox

__all__ = ["run_agent", "Tool", "Toolbox", "default_toolbox"]
