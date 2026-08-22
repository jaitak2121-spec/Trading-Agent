"""Trading system -- modular monolith.

Stage 1 contains ONLY the dependency-independent safety core. There is no
network I/O, no database, no web framework, and no exchange adapter in this
package yet. See docs/ARCHITECTURE.md.

LIVE TRADING IS DISABLED BY DEFAULT AND IS NOT IMPLEMENTED IN STAGE 1.
"""

__all__ = ["__version__"]

__version__ = "0.1.0-stage1"
