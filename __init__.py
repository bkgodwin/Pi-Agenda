"""Pi-Agenda application package."""

__version__ = "0.1.0"

# The repository root is not an installable package (setuptools uses src/), but
# pytest imports this file while collecting tests because it marks the checkout
# as a package. Use the canonical installed package instead of a relative import,
# which has no parent when the checkout directory contains a hyphen.
from pi_agenda import create_app

__all__ = ["__version__", "create_app"]
