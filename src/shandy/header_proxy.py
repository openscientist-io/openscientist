"""
HTTP proxy that strips anthropic-beta headers for OpenRouter compatibility.
"""
import http.server
import socketserver
import urllib.request
import urllib.error
import os
import logging

logger = logging.getLogger(__name__)

class HeaderStripProxy(http.server.BaseHTTPRequestHandler):
    """Proxy that removes anthropic-beta header before forwarding."""

    def do_POST(self):
        """Handle POST requests."""
        # Read the request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        # Get target URL from environment (use PROXY_TARGET_URL, fallback to ANTHROPIC_BASE_URL)
        target_url = os.getenv('PROXY_TARGET_URL') or os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
        full_url = f"{target_url}{self.path}"

        # Parse target to get correct Host header
        from urllib.parse import urlparse
        parsed = urlparse(target_url)
        target_host = parsed.netloc

        # Copy headers, excluding anthropic-beta and replacing Host
        headers = {}
        for key, value in self.headers.items():
            if key.lower() == 'anthropic-beta':
                continue  # Skip beta header
            elif key.lower() == 'host':
                headers['Host'] = target_host  # Replace with target host
            else:
                headers[key] = value

        logger.debug(f"Proxying POST to {full_url}")
        logger.debug(f"Target host: {target_host}")
        logger.debug(f"Stripped headers: {[k for k in self.headers.keys() if k.lower() == 'anthropic-beta']}")

        try:
            # Forward request without anthropic-beta header
            req = urllib.request.Request(
                full_url,
                data=body,
                headers=headers,
                method='POST'
            )

            with urllib.request.urlopen(req) as response:
                # Send response back to client
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.read())

        except urllib.error.HTTPError as e:
            # Forward error response
            self.send_response(e.code)
            for key, value in e.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(e.read())

        except Exception as e:
            logger.error(f"Proxy error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        """Override to use Python logging."""
        logger.info(format % args)


def start_proxy(port=8765):
    """Start the proxy server."""
    target = os.getenv('PROXY_TARGET_URL') or os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
    with socketserver.TCPServer(("localhost", port), HeaderStripProxy) as httpd:
        logger.info(f"Header-stripping proxy listening on localhost:{port}")
        logger.info(f"Forwarding to: {target}")
        httpd.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_proxy()
