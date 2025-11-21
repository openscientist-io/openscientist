# SHANDY Deployment Guide

This guide explains how to deploy SHANDY using Docker.

## Prerequisites

- Docker and Docker Compose installed
- CBORG API token (Anthropic API key via CBORG proxy)
- Claude Code CLI installed (in Docker container)

## Quick Start

### 1. Set up environment variables

Create a `.env` file in the project root:

```bash
ANTHROPIC_AUTH_TOKEN=your-cborg-token-here
CLAUDE_CLI_PATH=claude
```

### 2. Build and run with Docker Compose

```bash
# Production mode
docker-compose up -d

# Development mode (with code hot-reloading)
docker-compose -f docker-compose.dev.yml up
```

### 3. Access the web interface

Open your browser and navigate to:
```
http://localhost:8080
```

## Configuration

### Environment Variables

- `ANTHROPIC_AUTH_TOKEN`: CBORG API token (required)
- `CLAUDE_CLI_PATH`: Path to Claude Code CLI executable (default: `claude`)

### Volumes

The Docker setup mounts the following directories:

- `./jobs`: Persists discovery job data
- `./data`: Stores uploaded data files
- `./.env`: Environment variables (read-only)

### Ports

- `8080`: Web interface (NiceGUI)

## Claude Code CLI Installation

The Dockerfile attempts to install Claude Code CLI automatically. If this fails, you'll need to install it manually:

1. Stop the container:
   ```bash
   docker-compose down
   ```

2. Update the Dockerfile with the correct installation method

3. Rebuild:
   ```bash
   docker-compose build
   docker-compose up -d
   ```

Alternatively, mount a host-installed Claude Code CLI:

```yaml
# In docker-compose.yml
volumes:
  - /usr/local/bin/claude:/usr/local/bin/claude
```

## Managing Jobs

### View jobs

```bash
docker-compose exec shandy python -m shandy.job_manager list
```

### Get job details

```bash
docker-compose exec shandy python -m shandy.job_manager get <job_id>
```

### Delete a job

```bash
docker-compose exec shandy python -m shandy.job_manager delete <job_id>
```

### Clean up old jobs

```bash
# Delete jobs older than 7 days (keep completed)
docker-compose exec shandy python -m shandy.job_manager cleanup --days 7

# Delete all old jobs including completed
docker-compose exec shandy python -m shandy.job_manager cleanup --days 7 --delete-completed
```

## Monitoring

### View logs

```bash
# Follow logs
docker-compose logs -f

# View last 100 lines
docker-compose logs --tail 100
```

### Check container status

```bash
docker-compose ps
```

### Health check

The container includes a health check that pings the web interface every 30 seconds.

```bash
docker inspect shandy-shandy-1 --format='{{.State.Health.Status}}'
```

## Backup and Restore

### Backup jobs

```bash
# Create backup
tar -czf shandy-jobs-backup-$(date +%Y%m%d).tar.gz jobs/

# Copy out of container
docker cp shandy-shandy-1:/app/jobs ./jobs-backup
```

### Restore jobs

```bash
# Extract backup
tar -xzf shandy-jobs-backup-YYYYMMDD.tar.gz

# Copy into container
docker cp ./jobs shandy-shandy-1:/app/
```

## Troubleshooting

### Container won't start

1. Check logs:
   ```bash
   docker-compose logs
   ```

2. Verify environment variables:
   ```bash
   docker-compose config
   ```

3. Rebuild from scratch:
   ```bash
   docker-compose down -v
   docker-compose build --no-cache
   docker-compose up -d
   ```

### Claude Code CLI not found

1. Check if Claude CLI is installed in the container:
   ```bash
   docker-compose exec shandy which claude
   ```

2. If not found, install manually (see Claude Code CLI Installation above)

### Budget exceeded

Check your CBORG budget:

```bash
docker-compose exec shandy python -c "from shandy.cost_tracker import get_budget_info; print(get_budget_info())"
```

### Job stuck in "running" state

1. Check job manager:
   ```bash
   docker-compose exec shandy python -m shandy.job_manager get <job_id>
   ```

2. Check job logs:
   ```bash
   cat jobs/<job_id>/claude_iterations.log
   ```

3. Cancel the job:
   ```bash
   docker-compose exec shandy python -c "from shandy.job_manager import JobManager; JobManager().cancel_job('<job_id>')"
   ```

## Production Deployment

### Security Considerations

1. **Use secrets management**: Don't commit `.env` to git
   ```bash
   # Add to .gitignore
   echo ".env" >> .gitignore
   ```

2. **Restrict access**: Use a reverse proxy (nginx) with authentication
   ```nginx
   location / {
       auth_basic "SHANDY";
       auth_basic_user_file /etc/nginx/.htpasswd;
       proxy_pass http://localhost:8080;
   }
   ```

3. **Use HTTPS**: Set up SSL/TLS certificates
   ```bash
   certbot --nginx -d shandy.yourdomain.com
   ```

4. **Limit resource usage**: Add resource limits to docker-compose.yml
   ```yaml
   deploy:
     resources:
       limits:
         cpus: '2'
         memory: 4G
   ```

### Scaling

For multiple concurrent jobs, increase `max_concurrent` in `JobManager`:

```python
# In web_app.py init_app()
job_manager = JobManager(jobs_dir=jobs_dir, max_concurrent=3)
```

### Monitoring and Alerting

Set up monitoring with Prometheus/Grafana:

1. Add metrics endpoint to web app
2. Configure Prometheus to scrape metrics
3. Create Grafana dashboards for job status, costs, etc.

## Updating SHANDY

```bash
# Pull latest code
git pull origin main

# Rebuild and restart
docker-compose down
docker-compose build
docker-compose up -d
```

## Uninstalling

```bash
# Stop and remove containers
docker-compose down

# Remove volumes (WARNING: deletes all job data)
docker-compose down -v

# Remove images
docker rmi shandy-shandy
```

## Support

For issues or questions, open an issue on GitHub.
