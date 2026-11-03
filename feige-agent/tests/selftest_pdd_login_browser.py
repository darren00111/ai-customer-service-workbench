"""Real local browser/HTTP/WebSocket ordering; never visits a merchant service."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.channels.pinduoduo import Account, EVENTS, PinduoduoPilot
from app.config import StoreRegistry
from app.engine import browser
from app.service.workbench import Workbench


def main():
    exe = browser.find_chrome()
    if not exe:
        raise RuntimeError('Local browser integration test requires Edge or Chrome')
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/socket':
                value = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(value.encode()).digest()).decode())
                self.end_headers()
                data = b'local-login-order-fixture'
                self.wfile.write(bytes([0x81, len(data)]) + data)
                self.wfile.flush()
                stop.wait(15)
                self.close_connection = True
            else:
                data = b'<script>window.fixtureSocket=new WebSocket("ws://"+location.host+"/socket")</script>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    local_url = f'http://127.0.0.1:{server.server_port}/'
    proc, w = None, None
    temporary = tempfile.TemporaryDirectory(prefix='pdd-login-order-')
    try:
        directory = temporary.name
        root = Path(directory)
        w = Workbench(root/'reception', root/'stores', StoreRegistry(root/'registry.json', root/'stores'))
        w.create_store('local', 'Local protocol fixture', platform='pinduoduo')
        spec = w.spec('local')
        # This subclass permits only our loopback fixture. The shipped pilot
        # continues to enforce its exact HTTPS merchant origin and identity.
        class LocalPilot(PinduoduoPilot):
            def _chat_targets(self):
                return [t for t in self.cdp.list_targets() if t.get('type') == 'page' and t.get('url') == local_url]
        
            def _identity(self):
                return Account('100', '200', 'Local fixture')
        
            def _attach(self, target):
                super()._attach(target)
                self.audit = self.cdp.subscribe(EVENTS)
        
        pilot = LocalPilot(spec, root/'profiles', w.live)
        w._pilots['local'] = pilot
        def launch(*args, **kwargs):
            nonlocal proc
            assert args[2] == 'about:blank'
            proc = browser.launch_chrome(port, root/'browser', 'about:blank', chrome=exe,
                                         extra_args=['--headless=new', '--disable-gpu'])
            deadline = time.monotonic() + 15
            while not browser.cdp_alive(port):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Local browser failed to start')
                time.sleep(.1)
            return {'status':'launched'}
        
        with patch('app.channels.pinduoduo.browser.ensure_browser', side_effect=launch), patch('app.channels.pinduoduo.WORKSPACE', local_url):
            # Use the fixture port; no production/user browser is attached.
            pilot.cdp.port = port
            pilot.open_login()
            observed = set()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and 'Network.webSocketFrameReceived' not in observed:
                try:
                    event = pilot.audit.events.get(timeout=.2)
                except queue.Empty:
                    continue
                observed.add(event['method'])
            assert 'Network.webSocketCreated' in observed
            assert 'Network.webSocketFrameReceived' in observed
            stream = pilot.stream
            queued = stream.events.qsize()
            assert queued >= 2
            pilot.inspect()
            assert pilot.stream is stream and stream.events.qsize() >= queued
            assert not pilot.bound and not pilot.status()['automatic_sending']
            assert w.inbox.list(spec) == []
        report = {'ok':True, 'kind':'real_local_browser_websocket', 'network_observed_before_inspect':True,
                  'same_subscription_retained':True, 'login_started_sending':False,
                  'merchant_service_visited':False, 'real_shop_acceptance':False}
        (ROOT/'docs/verification/pdd-login-local-browser.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
    finally:
        if w:
            w.close_connections()
        stop.set()
        browser.kill_browser(port)
        if proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        server.shutdown()
        server.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    main()
