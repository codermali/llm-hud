"""Use the standard TOML parser when available, with a Python 3.9 fallback."""

try:
    from tomllib import TOMLDecodeError, loads
except ModuleNotFoundError:  # Python 3.9 and 3.10
    from llm_hud._vendor.tomli import TOMLDecodeError, loads

__all__ = ("TOMLDecodeError", "loads")
