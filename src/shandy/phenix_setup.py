"""Phenix environment setup for structural biology tools."""

import os
import subprocess
from typing import Optional


def setup_phenix_env() -> Optional[dict]:
    """
    Source Phenix environment and return updated environment dict.

    Returns:
        dict: Environment variables with Phenix paths, or None if PHENIX_PATH not set
    """
    phenix_path = os.getenv("PHENIX_PATH")
    if not phenix_path:
        return None

    if not os.path.exists(phenix_path):
        print(f"Warning: PHENIX_PATH {phenix_path} does not exist")
        return None

    env_script = os.path.join(phenix_path, "phenix_env.sh")
    if not os.path.exists(env_script):
        print(f"Warning: Phenix environment script not found at {env_script}")
        return None

    # Source the script and capture environment
    cmd = f"source {env_script} && env"
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            executable="/bin/bash",
            timeout=10,
            check=False,
        )

        # Parse environment variables
        phenix_env = os.environ.copy()
        for line in proc.stdout.split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                phenix_env[key] = value

        return phenix_env

    except subprocess.TimeoutExpired:
        print("Warning: Phenix environment setup timed out")
        return None
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Warning: Failed to setup Phenix environment: {e}")
        return None


def check_phenix_available() -> bool:
    """
    Check if Phenix is properly configured and available.

    Returns:
        bool: True if Phenix is available, False otherwise
    """
    return setup_phenix_env() is not None
