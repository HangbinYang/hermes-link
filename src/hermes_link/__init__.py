from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("hermes-link")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"

