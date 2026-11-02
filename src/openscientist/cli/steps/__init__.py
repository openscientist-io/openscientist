"""The ordered wizard steps."""

from openscientist.cli.step import Step
from openscientist.cli.steps.address import AddressStep
from openscientist.cli.steps.airgap import AirgapStep
from openscientist.cli.steps.concurrency import ConcurrencyStep
from openscientist.cli.steps.harness import HarnessStep
from openscientist.cli.steps.login import LoginStep
from openscientist.cli.steps.phenix import PhenixStep
from openscientist.cli.steps.provider import ProviderStep

STEPS: tuple[Step, ...] = (
    ProviderStep(),
    AddressStep(),
    HarnessStep(),
    ConcurrencyStep(),
    LoginStep(),
    AirgapStep(),
    PhenixStep(),
)

__all__ = [
    "STEPS",
    "AddressStep",
    "AirgapStep",
    "ConcurrencyStep",
    "HarnessStep",
    "LoginStep",
    "PhenixStep",
    "ProviderStep",
    "Step",
]
