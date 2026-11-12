"""Static file server for the tests.

- The first request to any path containing $THROTTLE gets HTTP 403.
- Every request to a path containing $FORBID gets HTTP 403.
- /azul/packages/ mimics Azul's metadata API for the archives in ./azul-cdn/.
"""

import hashlib
import http.server
import json
import os
import sys
from urllib.parse import unquote, urlsplit

seen, needle = set(), os.environ.get("THROTTLE", "")


class H(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        forbid = os.environ.get("FORBID", "")
        if forbid and forbid in self.path:  # permanently refused
            self.send_error(403)
            return
        if needle and needle in self.path and self.path not in seen:
            seen.add(self.path)
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path.startswith("/azul/packages/"):
            base = f"http://{self.headers['Host']}/azul-cdn/"
            uuid = unquote(path[len("/azul/packages/") :])
            names = sorted(os.listdir("azul-cdn"))
            if not uuid:  # list endpoint: filters ignored, the client matches by name
                return self._json(
                    [
                        {"name": n, "package_uuid": n, "download_url": base + n}
                        for n in names
                    ]
                )
            if uuid in names:
                with open(os.path.join("azul-cdn", uuid), "rb") as f:
                    data = f.read()
                return self._json(
                    {
                        "name": uuid,
                        "download_url": base + uuid,
                        "size": len(data),
                        "sha256_hash": hashlib.sha256(data).hexdigest(),
                    }
                )
            self.send_error(404)
            return
        super().do_GET()


os.chdir(sys.argv[2])
http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
