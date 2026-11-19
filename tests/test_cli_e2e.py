from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from typer.testing import CliRunner

from siwah.cli.main import app


def sse_body(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def usage() -> dict:
    return {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "opencode": {
            "billing_source": "go",
            "allowance": 0.0001,
            "tokens": {"input": 20, "output": 5, "cache_read": 0},
        },
    }


class MockProvider(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        index = MockProvider.calls
        MockProvider.calls += 1
        if index == 0:
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_read",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"math_utils.py"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage(),
                }
            ]
        elif index == 1:
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_edit",
                                        "function": {
                                            "name": "edit_file",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "math_utils.py",
                                                    "old_string": "return a - b",
                                                    "new_string": "return a + b",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage(),
                }
            ]
        else:
            chunks = [
                {
                    "choices": [
                        {"delta": {"content": "Fixed the failing test."}, "finish_reason": "stop"}
                    ],
                    "usage": usage(),
                }
            ]
        body = sse_body(chunks).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args) -> None:
        return


def test_cli_run_e2e_with_local_mock_provider(tmp_path: Path, monkeypatch, sample_repo: Path):
    MockProvider.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        config = tmp_path / "config.toml"
        config.write_text(
            f"""
[providers.mock]
api = "openai_chat"
base_url = "http://127.0.0.1:{port}/v1"
models_url = "http://127.0.0.1:{port}/v1/models"
auth = {{ scheme = "none" }}

[roles]
main = "mock/model-a"

[permissions]
mode = "auto-edit"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("SIWAH_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SIWAH_CONFIG", str(config))
        result = CliRunner().invoke(
            app,
            [
                "run",
                "Fix the failing test in math_utils.py",
                "--cwd",
                str(sample_repo),
                "--permission",
                "auto-edit",
                "--ui",
                "plain",
            ],
            input="a\n",
        )
        assert result.exit_code == 0, result.output
        assert "Fixed the failing test." in result.stdout
        assert "allowance" in result.stdout
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=sample_repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    finally:
        server.shutdown()
        server.server_close()
