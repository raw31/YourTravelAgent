"""Back-compat shim. Routing lives in `yta.profiles`, the pipeline in
`yta.pipeline`."""
from yta.pipeline import extract
from yta.profiles import route

__all__ = ["extract", "route"]
