#!/usr/bin/env python3
"""
AI Code Generator — single-file server

How to use:
1. Create a new GitHub repository and add this file as `server.py`, or run locally.
2. Set your OpenAI API key in the environment:
   export OPENAI_API_KEY="sk-..."
3. Run:
   python3 server.py
4. Open http://0.0.0.0:3000 in your browser.

Notes:
- This single file uses only Python standard library (tested on Python 3.8+).
- The server sends your prompt to OpenAI Chat Completions API and expects the model to
  return a JSON object with shape:
    {"files": [{"path":"relative/path/name.ext", "content":"...","encoding":"base64"(optional)} , ...]}
- Review generated code before running it.
- Do NOT expose this to the public internet with your key without authentication/rate-limits.
"""
import os
import io
import json
import base64
import zipfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "3000"))
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4")
API_TIMEOUT = 60  # seconds


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>AI Code Generator — Single File</title>
  <style>
    body { font-family: system-ui, -apple-system, Roboto, "Segoe UI", Arial; margin: 24px; max-width: 900px; }
    textarea { width: 100%; min-height: 160px; font-family: monospace; }
    label, select, input { display:block; margin-top: 12px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    button { margin-top: 12px; padding:8px 12px; }
    #status { margin-top: 12px; color: #333; white-space: pre-wrap; }
    .hint { background:#f7f7f7; padding:12px; border-radius:6px; margin-top:12px; }
  </style>
</head>
<body>
  <h1>AI Code Generator — Single File</h1>

  <label>Project name
    <input id="projectName" placeholder="my-app" />
  </label>

  <label>Language / Stack
    <select id="language">
      <option>any</option>
      <option>javascript</option>
      <option>nodejs</option>
      <option>python</option>
      <option>go</option>
      <option>rust</option>
      <option>java</option>
      <option>c</option>
      <option>c++</option>
      <option>html/css</option>
      <option>react</option>
      <option>vue</option>
    </select>
  </label>

  <label>Instructions (what you want the code to do)
    <textarea id="prompt" placeholder="e.g. create a REST API that returns current server time and a README"></textarea>
  </label>

  <div class="row">
    <button id="generate">Generate & Download ZIP</button>
    <button id="sample">Load sample prompt</button>
  </div>

  <div id="status"></div>

  <div class="hint">
    Tips:
    <ul>
      <li>Provide a project name and clear instructions. Ask for specific files (README, package.json, Dockerfile, etc.).</li>
      <li>Model outputs may require manual review before running.</li>
    </ul>
  </div>

  <script>
    const status = document.getElementById('status');
    document.getElementById('sample').addEventListener('click', () => {
      document.getElementById('projectName').value = 'time-api';
      document.getElementById('language').value = 'nodejs';
      document.getElementById('prompt').value = 'Create a small Node.js express app that exposes /time returning JSON { time: ISOString }. Include package.json and README with run instructions.';
    });

    document.getElementById('generate').addEventListener('click', async () => {
      status.textContent = 'Generating... this may take several seconds.';
      const prompt = document.getElementById('prompt').value;
      const language = document.getElementById('language').value;
      const projectName = document.getElementById('projectName').value || 'project';

      try {
        const res = await fetch('/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt, language, projectName })
        });

        if (!res.ok) {
          const err = await res.json().catch(()=>({ error: 'unknown' }));
          status.textContent = 'Error: ' + (err.error || JSON.stringify(err));
          return;
        }

        const contentType = res.headers.get('Content-Type') || '';
        if (contentType.includes('application/json')) {
          const body = await res.json();
          status.textContent = 'Server response: ' + JSON.stringify(body, null, 2);
          return;
        }

        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${projectName}.zip`;
        a.click();
        URL.revokeObjectURL(url);
        status.textContent = 'Download should start. Inspect the ZIP and run locally.';
      } catch (e) {
        status.textContent = 'Client error: ' + e.message;
      }
    });
  </script>
</body>
</html>
"""


def try_extract_json(text: str):
    """Try to extract the first JSON object substring from text and parse it."""
    if not text or not isinstance(text, str):
        return None
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    sub = text[first:last+1]
    try:
        return json.loads(sub)
    except Exception:
        return None


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "AICodeGen/0.1"

    def _set_cors_headers(self):
        # Keep it permissive for local use; tighten for production.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content = INDEX_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        if self.path != "/generate":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        if OPENAI_KEY is None:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            body = {"error": "Server missing OPENAI_API_KEY environment variable"}
            self.wfile.write(json.dumps(body).encode("utf-8"))
            return

        # Read request body
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        prompt = payload.get("prompt", "")
        language = payload.get("language", "any")
        project_name = payload.get("projectName", "project")

        # Build prompts
        system_prompt = (
            "You are a code generation assistant. Output JSON ONLY (no extra commentary) "
            "with this exact shape:\n"
            '{"files":[{"path":"relative/path/filename.ext","content":"file contents as a string"}]}\n'
            "Rules:\n"
            "- 'content' must be plain text. Escape characters in JSON as needed.\n"
            "- If a file is binary, include 'encoding':'base64' and put base64 data in 'content'.\n"
            "- Output ONLY the JSON object."
        )
        user_message = (
            f"Project name: {project_name}\n"
            f"Language/stack: {language}\n"
            f"Instructions: {prompt}\n\n"
            "Return a JSON object matching the schema above and nothing else."
        )

        # Prepare OpenAI API request
        openai_payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.2,
            "max_tokens": 2000,
        }
        req = urllib_request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(openai_payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_KEY}",
                "User-Agent": "ai-code-generator-single-file/0.1",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=API_TIMEOUT) as resp:
                result_text = resp.read().decode("utf-8")
                try:
                    result_json = json.loads(result_text)
                except Exception:
                    result_json = None
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print("OpenAI HTTPError:", e.code, err_body)
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "OpenAI API HTTP error", "details": err_body}).encode("utf-8"))
            return
        except URLError as e:
            print("OpenAI URLError:", e)
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "OpenAI API connection error", "details": str(e)}).encode("utf-8"))
            return
        except Exception as e:
            print("OpenAI request failed:", e)
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "OpenAI API request failed", "details": str(e)}).encode("utf-8"))
            return

        # Extract assistant message content
        assistant_text = None
        if result_json and "choices" in result_json and len(result_json["choices"]) > 0:
            # Chat completion schema: choices[0].message.content
            assistant_text = result_json["choices"][0].get("message", {}).get("content")
        if not assistant_text:
            # Fallback: try to parse raw response text for content field
            assistant_text = result_text

        # Try to parse JSON from assistant_text
        parsed = None
        try:
            parsed = json.loads(assistant_text)
        except Exception:
            parsed = try_extract_json(assistant_text)

        if not parsed or not isinstance(parsed.get("files"), list):
            # Return error JSON for debugging
            print("Failed to parse JSON from model. Raw output:\n", assistant_text)
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self._set_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            body = {
                "error": "Failed to parse files JSON from model response",
                "model_output": assistant_text
            }
            self.wfile.write(json.dumps(body).encode("utf-8"))
            return

        # Build ZIP in-memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in parsed["files"]:
                path = f.get("path")
                content = f.get("content")
                encoding = f.get("encoding")
                if not path or content is None:
                    continue
                if encoding == "base64":
                    try:
                        data = base64.b64decode(content)
                    except Exception:
                        # Skip bad entry
                        continue
                    zf.writestr(path, data)
                else:
                    # Write text (ensure it's str)
                    if not isinstance(content, str):
                        content = str(content)
                    zf.writestr(path, content.encode("utf-8"))

        zip_bytes = zip_buffer.getvalue()

        # Send ZIP response
        self.send_response(HTTPStatus.OK)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{project_name}.zip"')
        self.send_header("Content-Length", str(len(zip_bytes)))
        self.end_headers()
        self.wfile.write(zip_bytes)


def run_server():
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    print(f"Serving on http://{HOST}:{PORT}  (OpenAI model={OPENAI_MODEL})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    if OPENAI_KEY is None:
        print("WARNING: OPENAI_API_KEY is not set. Set it with:")
        print("  export OPENAI_API_KEY='sk-...'")
    run_server()
