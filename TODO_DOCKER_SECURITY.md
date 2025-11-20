# TODO: Docker Deployment for Security

## Current Status
- Running SHANDY locally for Phenix integration testing
- Using local Mac Phenix installation at `/Applications/phenix-1.21.2-5419`
- .env file loading fixed for local development

## Security Concerns
**IMPORTANT:** This is a temporary workaround for development. We need to run SHANDY in Docker for security reasons:
- Isolated execution environment for untrusted code execution
- Resource limits and sandboxing
- No direct access to host filesystem
- Better secrets management

## Blocking Issue
- ARM64 Mac cannot run x86_64 Docker images (Phenix is x86_64 only)
- "exec format error" when trying to build/run x86_64 containers

## Solutions to Investigate

### Option 1: Use Docker Desktop Rosetta emulation (RECOMMENDED)
- Docker Desktop for Mac supports Rosetta 2 emulation
- Enable in Docker Desktop settings: "Use Rosetta for x86/amd64 emulation on Apple Silicon"
- This should allow x86_64 containers to run

### Option 2: Multi-platform builds
- Build ARM64 version for local dev
- Build x86_64 version for production
- Use different Dockerfiles or build args

### Option 3: Find ARM64 Phenix
- Check if Phenix has an ARM64/Apple Silicon build
- Unlikely given it's specialized scientific software

### Option 4: Deploy to x86_64 server for testing
- Use the `gassh` server (x86_64) for all testing
- Slower feedback loop but maintains security

## Next Steps
1. Try enabling Rosetta in Docker Desktop settings
2. Rebuild Docker image with `make rebuild`
3. Test that Phenix tools work in container
4. Document the proper Docker deployment workflow
