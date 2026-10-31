"""The ordered wizard steps."""

from openscientist.cli.step import Step
from openscientist.cli.steps.address import AddressStep
from openscientist.cli.steps.concurrency import ConcurrencyStep
from openscientist.cli.steps.harness import HarnessStep
from openscientist.cli.steps.provider import ProviderStep

STEPS: tuple[Step, ...] = (ProviderStep(), AddressStep(), HarnessStep(), ConcurrencyStep())

__all__ = ["STEPS", "AddressStep", "ConcurrencyStep", "HarnessStep", "ProviderStep", "Step"]
