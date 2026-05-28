"""Exomiser environment setup for the variant-prioritization tool.

Only active when EXOMISER_PATH is configured. The Exomiser CLI jar + data bundle
live on the host and are bind-mounted into the agent container at /opt/exomiser
(see job_container/runner.py), mirroring the Phenix integration.
"""

import glob
import os
from typing import Any

from openscientist.settings import get_settings


def find_exomiser_jar(exomiser_path: str) -> str | None:
    """Return the Exomiser CLI jar under ``exomiser_path`` (highest version), or None."""
    jars = sorted(glob.glob(os.path.join(exomiser_path, "exomiser-cli-*.jar")))
    return jars[-1] if jars else None


def setup_exomiser_env() -> dict[str, Any] | None:
    """Build an environment dict for invoking Exomiser, or None if not configured."""
    exomiser_path = get_settings().exomiser.exomiser_path
    if not exomiser_path or not os.path.isdir(exomiser_path):
        return None
    if not find_exomiser_jar(exomiser_path):
        return None

    env = os.environ.copy()  # env-ok
    # The agent container sets JOB_ID=<uuid>; some JVM/queue utilities parse it as
    # an int and crash. Strip it from the env we hand to Exomiser (mirrors Phenix).
    env.pop("JOB_ID", None)
    return env


def check_exomiser_available() -> bool:
    """Return True if Exomiser is configured and the CLI jar is present."""
    try:
        return get_settings().exomiser.is_available
    except Exception:
        return setup_exomiser_env() is not None
