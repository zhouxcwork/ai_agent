"""通过 HTTP 验证课程 login_demo，运行前先执行 npm run build。"""

import json
from pathlib import Path
import queue
import subprocess
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = next(path for path in Path(__file__).resolve().parents if (path / "package.json").is_file())


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 直接启动项目的 Express 应用，由系统分配端口。
        app = """
const { createApp } = require('./dist/app.js');
const server = createApp().listen(0, '127.0.0.1', () => {
  console.log(server.address().port);
});
"""
        process = subprocess.Popen(
            ["node", "-e", app], cwd=ROOT, stdout=subprocess.PIPE, text=True
        )
        cls.addClassCleanup(cls.stop_app, process)
        assert process.stdout is not None
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        try:
            port = ready.get(timeout=10).strip()
        except queue.Empty as error:
            raise RuntimeError("login_demo 未在 10 秒内启动") from error
        if not port.isdigit():
            raise RuntimeError("login_demo 启动失败，请先运行 npm run build")
        cls.base_url = f"http://127.0.0.1:{port}"

    @staticmethod
    def stop_app(process):
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()

    def request(self, path, body=None, token=None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(self.base_url + path, data=payload, headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_login_token_can_access_profile(self):
        status, login = self.request("/api/login", {"email": "demo@example.com", "password": "demo123"})
        self.assertEqual(status, 200)
        status, profile = self.request("/api/profile", token=login["accessToken"])
        self.assertEqual(status, 200)
        self.assertEqual(profile["currentUser"]["userId"], "u_1001")

    def test_profile_requires_token(self):
        status, _ = self.request("/api/profile")
        self.assertEqual(status, 401)

    def test_invalid_credentials_are_rejected(self):
        status, _ = self.request("/api/login", {"email": "demo@example.com", "password": "wrong"})
        self.assertEqual(status, 401)
