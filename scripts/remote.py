from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class HTTPError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"OpenAI HTTP {status}: {message[:1500]}")


class OpenAIHTTP:
    def __init__(self, api_key, base="https://api.openai.com/v1"):
        if not api_key:
            raise ValueError("OPENAI_API_KEY mancante. Esegui INSTALLA.command.")
        self.key = api_key
        self.base = base.rstrip("/")

    def request(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Authorization": f"Bearer {self.key}", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", "replace").replace(self.key, "[chiave rimossa]")
            raise HTTPError(error.code, message) from error

    def json(self, method, path, body=None):
        return json.loads(self.request(method, path, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"} if body is not None else None))

    def upload(self, path: Path):
        boundary = "----schemi" + uuid.uuid4().hex
        content = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nbatch\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: application/jsonl\r\n\r\n'
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return json.loads(self.request("POST", "/files", content, {"Content-Type": f"multipart/form-data; boundary={boundary}"}))

    def create_batch(self, input_file_id, run_id, exam_id):
        return self.json("POST", "/batches", {"input_file_id": input_file_id, "endpoint": "/v1/responses", "completion_window": "24h", "metadata": {"pipeline": "schemi-esami-v2", "run_id": run_id, "exam_id": exam_id}})

    def create_urgent_response(self, body, run_id, exam_id):
        request = dict(body)
        request["background"] = True
        request["store"] = True
        request["metadata"] = {**request.get("metadata", {}), "pipeline": "schemi-esami-v2", "run_id": run_id, "exam_id": exam_id}
        return self.json("POST", "/responses", request)

    def get_response(self, response_id):
        return self.json("GET", "/responses/" + response_id)

    def list_models(self):
        return self.json("GET", "/models")

    def get_batch(self, batch_id):
        return self.json("GET", "/batches/" + batch_id)

    def download(self, file_id):
        return self.request("GET", "/files/" + file_id + "/content")

    def find_batch(self, input_file_id, run_id, exam_id):
        after = None
        # Ricerca paginata: non ricreare un invio dall'esito ambiguo.
        while True:
            query = {"limit": 100}
            if after:
                query["after"] = after
            page = self.json("GET", "/batches?" + urllib.parse.urlencode(query))
            for batch in page.get("data", []):
                meta = batch.get("metadata") or {}
                if batch.get("input_file_id") == input_file_id and meta.get("run_id") == run_id and meta.get("exam_id") == exam_id:
                    return batch
            if not page.get("has_more") or not page.get("data"):
                return None
            after = page["data"][-1]["id"]
