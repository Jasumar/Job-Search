"""Localhost review companion for the discovery review workflow.

This is a write-free view layer only. It serves a tiny control page that
advances one browser window to each candidate's careers URL as the CLI moves
through ``jobsearch_core discover review --interactive``. All accept/reject/skip
decisions stay in the CLI loop; the server only reports the current candidate
and renders the control page.

The server uses only the standard library (``http.server`` + ``webbrowser``).
It degrades gracefully: if the local server cannot bind a port, ``start()``
returns ``False`` and the caller proceeds headless. The companion stays
frontend-agnostic so the future web app (Phase 3.8) reuses the same
open-a-real-tab pattern (ATS boards block iframing) rather than reimplementing
review.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jobsearch_core.log import get_logger

logger = get_logger(__name__)

CONTROL_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Review companion</title>
<style>
  body { font-family: sans-serif; margin: 2em; }
  #status { font-size: 1.3em; font-weight: bold; }
  #fallback { background: #fff3cd; padding: 8px; border-radius: 4px; }
</style>
</head>
<body>
  <h1 id="status">Starting review...</h1>
  <p id="meta"></p>
  <iframe id="candidate" style="width:100%;height:68vh;border:1px solid #ccc;"></iframe>
  <p id="fallback">
    If the panel is blank, this hiring site blocks embedding.
    <a id="open" href="#" target="_blank" rel="noopener">Open the candidate in a new tab</a>.
  </p>
  <script>
    let lastKey = '';
    async function poll() {
      try {
        const res = await fetch('/state');
        const data = await res.json();
        if (data.status === 'complete') {
          document.getElementById('status').textContent = 'Review complete';
          document.getElementById('fallback').style.display = 'none';
          return;
        }
        const key = (data.index || 0) + '|' + (data.url || '');
        if (key !== lastKey) {
          lastKey = key;
          const meta = document.getElementById('meta');
          meta.textContent = data.label
            ? 'Reviewing ' + (data.index || 0) + ' of ' + (data.total || 0) + ': ' + data.label
            : '';
          document.getElementById('status').textContent =
            (data.index || 0) + ' of ' + (data.total || 0) +
            (data.label ? ' - ' + data.label : '');
          if (data.url) {
            document.getElementById('candidate').src = data.url;
            document.getElementById('open').href = data.url;
          }
        }
      } catch (err) {
        document.getElementById('status').textContent = 'Review companion unavailable';
      }
    }
    poll();
    setInterval(poll, 1500);
  </script>
</body>
</html>
"""


class _ReviewHandler(BaseHTTPRequestHandler):
    """Serves the control page and the current-candidate state."""

    companion: ReviewCompanion | None = None

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", CONTROL_PAGE.encode("utf-8"))
        elif self.path == "/state":
            state = self.companion.state() if self.companion else {"status": "complete"}
            self._respond(200, "application/json", json.dumps(state).encode("utf-8"))
        else:
            self.send_error(404)

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class ReviewCompanion:
    """A tiny localhost server that advances one browser window to each candidate.

    Start the server once, then call :meth:`set_current` as the user moves to
    each candidate. The control page polls ``/state`` and advances the same
    window to the new candidate. Call :meth:`mark_complete` when review ends so
    the page shows a "review complete" state.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._open_browser = open_browser
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {
            "status": "active",
            "url": None,
            "label": "",
            "index": 0,
            "total": 0,
        }

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def start(self) -> bool:
        """Bind the local server and open the control page in the browser.

        Returns ``False`` (and never raises) when the server cannot start, so
        the caller can proceed headless.
        """
        if self._server is not None:
            return True
        try:
            _ReviewHandler.companion = self
            self._server = ThreadingHTTPServer((self._host, self._port), _ReviewHandler)
            self._port = int(self._server.server_address[1])
        except OSError as exc:
            logger.warning("Review companion could not start: %s", exc)
            self.close()
            return False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self._open_browser:
            webbrowser.open(self.base_url)
        return True

    def set_current(self, url: str, label: str = "", index: int = 0, total: int = 0) -> None:
        with self._lock:
            self._state.update(
                status="active",
                url=url,
                label=label,
                index=index,
                total=total,
            )

    def mark_complete(self) -> None:
        with self._lock:
            self._state.update(status="complete", url=None)

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except OSError:
                pass
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
