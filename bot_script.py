import os
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

def do_nothing():
    while True:
        time.sleep(60)

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    t = threading.Thread(target=do_nothing, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 10000))
    print(f"Binding on {port}", flush=True)
    HTTPServer(("0.0.0.0", port), H).serve_forever()
