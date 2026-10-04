"""QueryAPIGate - expose SQL databases as a REST API."""
__version__ = '0.13.0'

from .app import create_app  # noqa: E402  (app imports __version__)

__all__ = ['create_app', '__version__']
