#!/usr/bin/env python
"""Quick test script to verify Azure Foundry provider configuration."""

import os
from pathlib import Path

# Load .env file
from dotenv import load_dotenv
load_dotenv()

print("Environment variables loaded from .env:")
print(f"  CLAUDE_PROVIDER: {os.getenv('CLAUDE_PROVIDER')}")
print(f"  ANTHROPIC_FOUNDRY_RESOURCE: {os.getenv('ANTHROPIC_FOUNDRY_RESOURCE')}")
print(f"  ANTHROPIC_FOUNDRY_API_KEY: {'***' + os.getenv('ANTHROPIC_FOUNDRY_API_KEY', '')[-8:] if os.getenv('ANTHROPIC_FOUNDRY_API_KEY') else 'Not set'}")
print()

# Test provider
from shandy.providers import get_provider

try:
    provider = get_provider()
    print(f"✅ Provider loaded: {provider.name}")
    print()

    # If we got here, configuration is valid (validation happens in __init__)
    print("✅ Configuration validated successfully!")
    print()

    print("🎉 Azure Foundry provider is configured correctly!")
    print()
    print("Available models:")
    print(f"  - {os.getenv('ANTHROPIC_DEFAULT_OPUS_MODEL', 'claude-opus-4-6')} (Opus)")
    print(f"  - {os.getenv('ANTHROPIC_DEFAULT_SONNET_MODEL', 'claude-sonnet-4-5')} (Sonnet)")
    print(f"  - {os.getenv('ANTHROPIC_DEFAULT_HAIKU_MODEL', 'claude-haiku-4-5')} (Haiku)")
    print()
    print("Next steps:")
    print("  1. Run: uv run python -m shandy.web_app")
    print("  2. Open: http://localhost:8080")
    print("  3. Create a job and test the Azure Foundry integration!")

except ValueError as e:
    print(f"❌ Configuration error: {e}")
    print()
    print("Please check your .env file configuration.")
