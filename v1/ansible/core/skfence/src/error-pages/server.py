#!/usr/bin/env python3

# Copyright (C) 2025 S&K Holding QT (Quantum Technologies)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
SKStacks Error Pages Server
Serves custom error pages with hidden debug information
Uses docker-socket-proxy for secure Docker API access
"""

import os
import json
import socket
import subprocess
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import psutil
import urllib.request
import urllib.error

# Try to import docker library, fallback to HTTP requests if not available
try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

# Docker Socket Proxy Configuration
def get_socket_proxy_url():
    """Get the docker-socket-proxy URL based on environment"""
    app_env = os.getenv('APP_ENV', 'prod')
    return f"http://tasks.socket-proxy:2375"

def docker_api_request(endpoint, method='GET', data=None):
    """Make HTTP request to docker-socket-proxy"""
    try:
        url = f"{get_socket_proxy_url()}{endpoint}"
        req = urllib.request.Request(url)
        req.get_method = lambda: method
        
        if data:
            req.data = json.dumps(data).encode('utf-8')
            req.add_header('Content-Type', 'application/json')
        
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return None

# Error messages mapping
ERROR_MESSAGES = {
    '400': {
        'title': 'Bad Request',
        'message': 'The request was invalid or malformed. Please check your input and try again.'
    },
    '401': {
        'title': 'Unauthorized',
        'message': 'Authentication is required to access this resource. Please log in and try again.'
    },
    '403': {
        'title': 'Access Forbidden',
        'message': 'You don\'t have permission to access this resource. This may be due to security restrictions or access control policies.'
    },
    '404': {
        'title': 'Page Not Found',
        'message': 'The page you\'re looking for doesn\'t exist. It may have been moved, deleted, or the URL might be incorrect.'
    },
    '405': {
        'title': 'Method Not Allowed',
        'message': 'The HTTP method used is not allowed for this resource.'
    },
    '408': {
        'title': 'Request Timeout',
        'message': 'The request took too long to complete. Please try again.'
    },
    '409': {
        'title': 'Conflict',
        'message': 'The request conflicts with the current state of the resource.'
    },
    '429': {
        'title': 'Too Many Requests',
        'message': 'You have made too many requests. Please wait a moment and try again.'
    },
    '500': {
        'title': 'Internal Server Error',
        'message': 'Something went wrong on our end. Our team has been notified and is working to fix the issue.'
    },
    '502': {
        'title': 'Bad Gateway',
        'message': 'The server received an invalid response from an upstream server. Please try again later.'
    },
    '503': {
        'title': 'Service Unavailable',
        'message': 'The service is temporarily unavailable. This is usually due to maintenance or high load. Please try again later.'
    },
    '504': {
        'title': 'Gateway Timeout',
        'message': 'The server did not receive a timely response from an upstream server. Please try again later.'
    }
}

# Default error message
DEFAULT_ERROR = {
    'title': 'Error',
    'message': 'An error occurred while processing your request.'
}


def get_container_info():
    """Get Docker container information"""
    info = {
        'container_id': 'unknown',
        'container_name': 'unknown',
        'service_name': 'unknown',
        'container_image': 'unknown',
        'hostname': socket.gethostname()
    }
    
    try:
        # Try to read container ID from /proc/self/cgroup
        with open('/proc/self/cgroup', 'r') as f:
            for line in f:
                if 'docker' in line or 'containerd' in line:
                    parts = line.strip().split('/')
                    if len(parts) > 2:
                        container_id = parts[-1].split('-')[0]
                        if len(container_id) == 64:
                            info['container_id'] = container_id[:12]
                            break
        
        # Try to get container name from hostname (Docker Swarm format)
        hostname = socket.gethostname()
        if '.' in hostname:
            # Format: service_name.task_id.slot_id
            parts = hostname.split('.')
            info['service_name'] = parts[0]
            info['container_name'] = hostname
        
        # Try to get image from environment or /proc/self/environ
        if os.path.exists('/proc/self/environ'):
            with open('/proc/self/environ', 'r') as f:
                env_data = f.read()
                if 'IMAGE=' in env_data:
                    for line in env_data.split('\0'):
                        if line.startswith('IMAGE='):
                            info['container_image'] = line.split('=', 1)[1]
                            break
    except Exception as e:
        pass
    
    return info


def get_node_info():
    """Get Docker Swarm node information using docker-socket-proxy"""
    info = {
        'node_id': 'unknown',
        'node_hostname': socket.gethostname(),
        'node_role': 'unknown',
        'node_labels': '{}',
        'node_address': 'unknown',
        'node_name': 'unknown'  # Extract node label (e.g., node01, node02, etc.)
    }
    
    try:
        container_hostname = socket.gethostname()
        
        # Get current container ID
        container_id = None
        try:
            with open('/proc/self/cgroup', 'r') as f:
                for line in f:
                    if 'docker' in line or 'containerd' in line:
                        parts = line.strip().split('/')
                        if len(parts) > 2:
                            cid = parts[-1].split('-')[0]
                            if len(cid) == 64:
                                container_id = cid[:12]
                                break
        except Exception:
            pass
        
        # Get container info to find NodeID
        if container_id:
            container_data = docker_api_request(f'/containers/{container_id}/json')
            if container_data:
                node_id = container_data.get('NodeID', '')
                if not node_id:
                    # Try to get from labels
                    labels = container_data.get('Config', {}).get('Labels', {})
                    node_id = labels.get('com.docker.swarm.node.id', '')
                
                if node_id:
                    # Get node info
                    node_data = docker_api_request(f'/nodes/{node_id}')
                    if node_data:
                        desc = node_data.get('Description', {})
                        spec = node_data.get('Spec', {})
                        
                        info['node_id'] = node_id[:12] if len(node_id) > 12 else node_id
                        info['node_hostname'] = desc.get('Hostname', container_hostname)
                        
                        # Get role
                        if 'ManagerStatus' in node_data:
                            info['node_role'] = 'manager'
                        else:
                            info['node_role'] = 'worker'
                        
                        # Get labels
                        labels = spec.get('Labels', {})
                        info['node_labels'] = json.dumps(labels, indent=2)
                        
                        # Extract node name from labels (e.g., node=node01)
                        if 'node' in labels:
                            info['node_name'] = labels['node']
                        
                        # Get address
                        if 'Address' in desc:
                            info['node_address'] = desc['Address']
                        return info
        
        # Fallback: Get all nodes and find matching one
        nodes_data = docker_api_request('/nodes')
        if nodes_data:
            matched_node = None
            
            # First, try to match by hostname
            for node in nodes_data:
                desc = node.get('Description', {})
                node_hostname = desc.get('Hostname', '')
                if node_hostname in container_hostname or container_hostname in node_hostname:
                    matched_node = node
                    break
            
            # If no match, use first node with 'node' label
            if not matched_node:
                for node in nodes_data:
                    spec = node.get('Spec', {})
                    labels = spec.get('Labels', {})
                    if 'node' in labels:
                        matched_node = node
                        break
            
            # If still no match, use first node
            if not matched_node and nodes_data:
                matched_node = nodes_data[0]
            
            if matched_node:
                desc = matched_node.get('Description', {})
                spec = matched_node.get('Spec', {})
                labels = spec.get('Labels', {})
                
                info['node_id'] = matched_node.get('ID', 'unknown')[:12]
                info['node_hostname'] = desc.get('Hostname', container_hostname)
                
                # Get role
                if 'ManagerStatus' in matched_node:
                    info['node_role'] = 'manager'
                else:
                    info['node_role'] = 'worker'
                
                info['node_labels'] = json.dumps(labels, indent=2)
                
                # Extract node name from labels
                if 'node' in labels:
                    info['node_name'] = labels['node']
                
                # Get address
                if 'Address' in desc:
                    info['node_address'] = desc['Address']
    except Exception as e:
        pass
    
    return info


def get_network_info():
    """Get network information using docker-socket-proxy"""
    info = {
        'network_mode': 'unknown',
        'networks': '[]'
    }
    
    try:
        container_id = get_container_info()['container_id']
        if container_id != 'unknown':
            container_data = docker_api_request(f'/containers/{container_id}/json')
            if container_data:
                net_settings = container_data.get('NetworkSettings', {})
                info['network_mode'] = net_settings.get('NetworkMode', 'unknown')
                networks = net_settings.get('Networks', {})
                info['networks'] = json.dumps(list(networks.keys()), indent=2)
    except Exception as e:
        pass
    
    return info

def get_cluster_info():
    """Get comprehensive cluster information using docker-socket-proxy"""
    cluster_info = {
        'swarm_info': {},
        'all_nodes': [],
        'all_services': [],
        'all_stacks': [],
        'all_networks': [],
        'all_volumes': []
    }
    
    try:
        # Get Swarm info
        swarm_info = docker_api_request('/swarm')
        if swarm_info:
            cluster_info['swarm_info'] = {
                'cluster_id': swarm_info.get('ID', 'unknown')[:12] if swarm_info.get('ID') else 'unknown',
                'join_token_worker': swarm_info.get('JoinTokens', {}).get('Worker', '')[:20] + '...' if swarm_info.get('JoinTokens', {}).get('Worker') else 'unknown',
                'join_token_manager': swarm_info.get('JoinTokens', {}).get('Manager', '')[:20] + '...' if swarm_info.get('JoinTokens', {}).get('Manager') else 'unknown'
            }
        
        # Get all nodes
        nodes_data = docker_api_request('/nodes')
        if nodes_data:
            for node in nodes_data:
                desc = node.get('Description', {})
                spec = node.get('Spec', {})
                status = node.get('Status', {})
                labels = spec.get('Labels', {})
                
                node_info = {
                    'id': node.get('ID', 'unknown')[:12],
                    'hostname': desc.get('Hostname', 'unknown'),
                    'role': 'manager' if 'ManagerStatus' in node else 'worker',
                    'status': status.get('State', 'unknown'),
                    'availability': spec.get('Availability', 'unknown'),
                    'node_name': labels.get('node', 'unknown'),
                    'address': desc.get('Address', 'unknown'),
                    'platform': desc.get('Platform', {}).get('Architecture', 'unknown')
                }
                cluster_info['all_nodes'].append(node_info)
        
        # Get all services
        services_data = docker_api_request('/services')
        if services_data:
            for service in services_data:
                spec = service.get('Spec', {})
                service_info = {
                    'id': service.get('ID', 'unknown')[:12],
                    'name': spec.get('Name', 'unknown'),
                    'mode': 'replicated' if spec.get('Mode', {}).get('Replicated') else 'global',
                    'replicas': spec.get('Mode', {}).get('Replicated', {}).get('Replicas', 'N/A'),
                    'image': spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', 'unknown')
                }
                cluster_info['all_services'].append(service_info)
        
        # Get all networks
        networks_data = docker_api_request('/networks')
        if networks_data:
            for network in networks_data:
                network_info = {
                    'id': network.get('Id', 'unknown')[:12],
                    'name': network.get('Name', 'unknown'),
                    'driver': network.get('Driver', 'unknown'),
                    'scope': network.get('Scope', 'unknown'),
                    'internal': network.get('Internal', False)
                }
                cluster_info['all_networks'].append(network_info)
        
        # Get all volumes
        volumes_data = docker_api_request('/volumes')
        if volumes_data and 'Volumes' in volumes_data:
            for volume in volumes_data['Volumes']:
                volume_info = {
                    'name': volume.get('Name', 'unknown'),
                    'driver': volume.get('Driver', 'unknown'),
                    'mountpoint': volume.get('Mountpoint', 'unknown')
                }
                cluster_info['all_volumes'].append(volume_info)
        
        # Extract stack names from services (services are named like stack-name_service-name)
        stacks = set()
        for service in cluster_info['all_services']:
            service_name = service.get('name', '')
            if '_' in service_name:
                stack_name = service_name.split('_')[0]
                stacks.add(stack_name)
        cluster_info['all_stacks'] = sorted(list(stacks))
        
    except Exception as e:
        pass
    
    return cluster_info


def get_system_metrics():
    """Get system metrics"""
    info = {
        'uptime': 'unknown',
        'memory_usage': 'unknown',
        'cpu_usage': 'unknown'
    }
    
    try:
        # Uptime
        uptime_seconds = time.time() - psutil.boot_time()
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        info['uptime'] = f'{hours}h {minutes}m'
        
        # Memory
        memory = psutil.virtual_memory()
        info['memory_usage'] = f'{memory.percent:.1f}% ({memory.used / (1024**3):.2f} GB / {memory.total / (1024**3):.2f} GB)'
        
        # CPU
        cpu_percent = psutil.cpu_percent(interval=0.1)
        info['cpu_usage'] = f'{cpu_percent:.1f}%'
    except Exception as e:
        pass
    
    return info


def get_env_info():
    """Get environment information"""
    return {
        'app_env': os.getenv('APP_ENV', 'unknown'),
        'cluster_name': os.getenv('CLUSTERNAME', 'unknown'),
        'domain': os.getenv('DOMAIN', 'unknown')
    }


class ErrorPageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET requests"""
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        
        # Extract status code from query parameter
        status_code = query_params.get('status', [None])[0]
        if isinstance(status_code, list):
            status_code = status_code[0] if status_code else None
        
        # Handle /error/{status} format
        if status_code is None and parsed_path.path.startswith('/error/'):
            status_code = parsed_path.path.split('/')[-1]
        
        # Default to 404 for catch-all routes (no status code specified)
        if status_code is None:
            status_code = '404'
        
        # Get error message
        error_info = ERROR_MESSAGES.get(status_code, DEFAULT_ERROR)
        
        # Gather all debug information
        container_info = get_container_info()
        node_info = get_node_info()
        network_info = get_network_info()
        system_metrics = get_system_metrics()
        env_info = get_env_info()
        cluster_info = get_cluster_info()  # Comprehensive cluster information
        
        # Get request information
        client_ip = self.headers.get('X-Real-Ip') or self.headers.get('X-Forwarded-For') or self.client_address[0]
        user_agent = self.headers.get('User-Agent', 'Unknown')
        request_method = self.command
        request_path = parsed_path.path
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        
        # Build debug JSON
        debug_data = {
            'request': {
                'status_code': status_code,
                'path': request_path,
                'method': request_method,
                'client_ip': client_ip,
                'user_agent': user_agent,
                'timestamp': timestamp
            },
            'container': container_info,
            'node': {
                'node_id': node_info['node_id'],
                'node_hostname': node_info['node_hostname'],
                'node_role': node_info['node_role'],
                'node_name': node_info['node_name'],  # Node label (e.g., node01)
                'node_labels': json.loads(node_info['node_labels']) if node_info['node_labels'] != '{}' else {},
                'node_address': node_info['node_address']
            },
            'network': network_info,
            'system': system_metrics,
            'environment': env_info,
            'cluster': cluster_info  # Comprehensive cluster information
        }
        
        # Load and render HTML template
        template_path = '/app/error.html'
        if not os.path.exists(template_path):
            template_path = os.path.join(os.path.dirname(__file__), 'error.html')
        
        try:
            with open(template_path, 'r') as f:
                html = f.read()
            
            # Replace template variables
            html = html.replace('{{ status_code }}', status_code)
            html = html.replace('{{ error_title }}', error_info['title'])
            html = html.replace('{{ error_message }}', error_info['message'])
            html = html.replace('{{ request_path }}', request_path)
            html = html.replace('{{ request_method }}', request_method)
            html = html.replace('{{ client_ip }}', client_ip)
            html = html.replace('{{ user_agent }}', user_agent)
            html = html.replace('{{ timestamp }}', timestamp)
            html = html.replace('{{ container_id }}', container_info['container_id'])
            html = html.replace('{{ container_name }}', container_info['container_name'])
            html = html.replace('{{ service_name }}', container_info['service_name'])
            html = html.replace('{{ container_image }}', container_info['container_image'])
            html = html.replace('{{ hostname }}', container_info['hostname'])
            html = html.replace('{{ node_id }}', node_info['node_id'])
            html = html.replace('{{ node_hostname }}', node_info['node_hostname'])
            html = html.replace('{{ node_role }}', node_info['node_role'])
            html = html.replace('{{ node_name }}', node_info['node_name'])
            html = html.replace('{{ node_labels }}', node_info['node_labels'])
            html = html.replace('{{ node_address }}', node_info['node_address'])
            html = html.replace('{{ network_mode }}', network_info['network_mode'])
            html = html.replace('{{ networks }}', network_info['networks'])
            html = html.replace('{{ app_env }}', env_info['app_env'])
            html = html.replace('{{ cluster_name }}', env_info['cluster_name'])
            html = html.replace('{{ domain }}', env_info['domain'])
            html = html.replace('{{ uptime }}', system_metrics['uptime'])
            html = html.replace('{{ memory_usage }}', system_metrics['memory_usage'])
            html = html.replace('{{ cpu_usage }}', system_metrics['cpu_usage'])
            
            # Cluster information replacements
            cluster_swarm_id = cluster_info.get('swarm_info', {}).get('cluster_id', 'unknown')
            cluster_nodes_count = str(len(cluster_info.get('all_nodes', [])))
            cluster_services_count = str(len(cluster_info.get('all_services', [])))
            cluster_stacks_count = str(len(cluster_info.get('all_stacks', [])))
            cluster_networks_count = str(len(cluster_info.get('all_networks', [])))
            cluster_volumes_count = str(len(cluster_info.get('all_volumes', [])))
            cluster_nodes_json = json.dumps(cluster_info.get('all_nodes', []), indent=2)
            cluster_services_json = json.dumps(cluster_info.get('all_services', []), indent=2)
            cluster_stacks_json = json.dumps(cluster_info.get('all_stacks', []), indent=2)
            cluster_networks_json = json.dumps(cluster_info.get('all_networks', []), indent=2)
            cluster_volumes_json = json.dumps(cluster_info.get('all_volumes', []), indent=2)
            
            html = html.replace('{{ cluster_swarm_id }}', cluster_swarm_id)
            html = html.replace('{{ cluster_nodes_count }}', cluster_nodes_count)
            html = html.replace('{{ cluster_services_count }}', cluster_services_count)
            html = html.replace('{{ cluster_stacks_count }}', cluster_stacks_count)
            html = html.replace('{{ cluster_networks_count }}', cluster_networks_count)
            html = html.replace('{{ cluster_volumes_count }}', cluster_volumes_count)
            html = html.replace('{{ cluster_nodes_json }}', cluster_nodes_json)
            html = html.replace('{{ cluster_services_json }}', cluster_services_json)
            html = html.replace('{{ cluster_stacks_json }}', cluster_stacks_json)
            html = html.replace('{{ cluster_networks_json }}', cluster_networks_json)
            html = html.replace('{{ cluster_volumes_json }}', cluster_volumes_json)
            html = html.replace('{{ debug_json }}', json.dumps(debug_data, indent=2))
            
            # Send response
            self.send_response(int(status_code))
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html.encode('utf-8'))))
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        except Exception as e:
            # Fallback error response
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(f'Error generating error page: {str(e)}'.encode('utf-8'))
    
    def log_message(self, format, *args):
        """Suppress default logging"""
        pass


def main():
    """Main server function"""
    port = int(os.getenv('PORT', '8080'))
    server = HTTPServer(('0.0.0.0', port), ErrorPageHandler)
    print(f'SKStacks Error Pages Server listening on port {port}')
    server.serve_forever()


if __name__ == '__main__':
    main()

