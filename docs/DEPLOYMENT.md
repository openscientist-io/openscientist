# OpenScientist Deployment Guide

This guide explains how to deploy OpenScientist using Docker.

## Prerequisites

- Docker and Docker Compose installed
- LLM provider credentials (see `.env.example` for options)

## Quick Start

### 1. Set up environment variables

Create a `.env` file in the project root:

```bash
cp .env.example .env
# Edit .env with your provider credentials
```

### 2. Build and run with Docker Compose

```bash
# Build and start
make build
make start
```

### 3. Access the web interface

Open your browser and navigate to:
```
http://localhost:8080
```

## Configuration

### Environment Variables

See `.env.example` for the full list of configuration options.

### Volumes

The Docker setup mounts the following directories:

- `./jobs`: Persists discovery job data
- `./data`: Stores uploaded data files
- `./.env`: Environment variables (read-only)

### Ports

- `8080`: Web interface (NiceGUI)

## Managing Jobs

### View jobs

```bash
docker-compose exec open_scientist python -m open_scientist.job_manager list
```

### Get job details

```bash
docker-compose exec open_scientist python -m open_scientist.job_manager get <job_id>
```

### Delete a job

```bash
docker-compose exec open_scientist python -m open_scientist.job_manager delete <job_id>
```

### Clean up old jobs

```bash
# Delete jobs older than 7 days (keep completed)
docker-compose exec open_scientist python -m open_scientist.job_manager cleanup --days 7

# Delete all old jobs including completed
docker-compose exec open_scientist python -m open_scientist.job_manager cleanup --days 7 --delete-completed
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
docker inspect open_scientist-open_scientist-1 --format='{{.State.Health.Status}}'
```

## Backup and Restore

### Backup jobs

```bash
# Create backup
tar -czf open_scientist-jobs-backup-$(date +%Y%m%d).tar.gz jobs/

# Copy out of container
docker cp open_scientist-open_scientist-1:/app/jobs ./jobs-backup
```

### Restore jobs

```bash
# Extract backup
tar -xzf open_scientist-jobs-backup-YYYYMMDD.tar.gz

# Copy into container
docker cp ./jobs open_scientist-open_scientist-1:/app/
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

### Budget exceeded

Check your budget:

```bash
docker-compose exec open_scientist python -c "from open_scientist.cost_tracker import get_budget_info; print(get_budget_info())"
```

### Job stuck in "running" state

1. Check job manager:
   ```bash
   docker-compose exec open_scientist python -m open_scientist.job_manager get <job_id>
   ```

2. Check job logs:
   ```bash
   cat jobs/<job_id>/claude_iterations.log
   ```

3. Cancel the job:
   ```bash
   docker-compose exec open_scientist python -c "from open_scientist.job_manager import JobManager; JobManager().cancel_job('<job_id>')"
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
       auth_basic "OpenScientist";
       auth_basic_user_file /etc/nginx/.htpasswd;
       proxy_pass http://localhost:8080;
   }
   ```

3. **Use HTTPS**: Set up SSL/TLS certificates
   ```bash
   certbot --nginx -d open-scientist.yourdomain.com
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

## Updating OpenScientist

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
docker rmi open_scientist-open_scientist
```

## Support

For issues or questions, open an issue on GitHub.
