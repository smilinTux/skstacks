# SKStacks Error Pages Service

## Overview

The SKStacks Error Pages service provides modern, branded error pages with hidden debug information for troubleshooting HA/multi-node and single-node errors.

## Features

- **Modern Design**: Sleek, dark-themed error pages matching SKStacks branding
- **Hidden Debug Info**: Comprehensive troubleshooting metrics accessible via:
  - **Ctrl+D** (or Cmd+D on Mac) keyboard shortcut
  - **Triple-click** on the error code
  - **Toggle button** at the bottom of the page
- **Comprehensive Metrics**: Includes:
  - Request information (status code, path, method, client IP, user agent, timestamp)
  - Container information (ID, name, service name, image, hostname)
  - Node information (ID, hostname, role, node name, labels, address)
  - Network information (mode, networks)
  - Environment information (app env, cluster name, domain)
  - System metrics (uptime, memory usage, CPU usage)
  - **Cluster Information** (gathered via docker-socket-proxy):
    - Swarm Cluster ID
    - All nodes (ID, hostname, role, status, availability, node name, address, platform)
    - All services (ID, name, mode, replicas, image)
    - All stacks (extracted from service names)
    - All networks (ID, name, driver, scope, internal flag)
    - All volumes (name, driver, mountpoint)
  - Full debug JSON dump

## Architecture

- **Service**: Python HTTP server serving custom error pages
- **Template**: Jinja2-style HTML template with embedded variables
- **Docker**: Lightweight Python 3.11-slim image with psutil and urllib libraries
- **Integration**: Integrated into Traefik error-pages middleware
- **Docker API Access**: Uses docker-socket-proxy for secure cluster information gathering
  - Connects to `http://tasks.socket-proxy:2375`
  - Gathers comprehensive cluster information via HTTP API
  - More secure than direct Docker socket access

## Error Codes Supported

- **403**: Access Forbidden
- **404**: Page Not Found
- **500**: Internal Server Error
- **503**: Service Unavailable

## Usage

The error pages are automatically served by Traefik when errors occur. The service is configured in:

- `skfence.yml.j2`: Docker Compose service definition
- `middlewares.yml.j2`: Traefik error-pages middleware configuration
- `services.yml.j2`: Traefik error-pages service definition

## Debug Information Access

### Method 1: Keyboard Shortcut
Press **Ctrl+D** (or **Cmd+D** on Mac) to toggle debug information.

### Method 2: Triple-Click
Triple-click on the error code (e.g., "404") to show/hide debug information.

### Method 3: Toggle Button
Click the "🔍 Show Debug Info" button at the bottom of the page.

## Configuration

The service reads environment variables:
- `PORT`: Server port (default: 8080)
- `APP_ENV`: Application environment (prod/staging/dev)
- `CLUSTERNAME`: Cluster name
- `DOMAIN`: Domain name

## Deployment

The error-pages service is automatically deployed as part of the skfence stack. Files are deployed to:
- `/var/data/config/skfence-{env}/error-pages/`

## Troubleshooting

If debug information shows "unknown" values:
1. **Cluster Information Issues**:
   - Verify error-pages service is on `cloud-public-{env}` network
   - Check docker-socket-proxy is running: `docker service ps skfence-{env}_socket-proxy`
   - Verify socket-proxy is accessible: `curl http://tasks.socket-proxy:2375/info`
   - Check service logs for API errors: `docker service logs skfence-{env}_error-pages`
2. **Container/Node Information Issues**:
   - Ensure Docker socket is mounted: `/var/run/docker.sock:/var/run/docker.sock:ro` (fallback)
   - Verify environment variables are set correctly
   - Check service logs: `docker service logs skfence-{env}_error-pages`

## Security

- Debug information is hidden by default
- Only accessible via explicit user action (keyboard shortcut, button, or triple-click)
- No sensitive information is exposed in the default view
- **Docker API Access**: Uses docker-socket-proxy instead of direct socket access
  - More secure: Restricted API access via HTTP
  - Network isolation: Service communicates over overlay network
  - Permission-based: Socket-proxy enforces API endpoint restrictions
- Docker socket is mounted read-only (fallback only)

## Branding

The error pages use the SKStacks logo from:
- `https://smilinTux.org/logos/skstacks-banner.png`

If the logo fails to load, it gracefully hides without breaking the page layout.

