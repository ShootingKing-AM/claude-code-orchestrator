import io
import os
import pytest
from fastapi.testclient import TestClient
from web.server import app

client = TestClient(app)


def test_upload_single_file():
    data = {"files": ("hello.txt", io.BytesIO(b"hello world"), "text/plain")}
    res = client.post("/api/upload", files=data)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["name"] == "hello.txt"
    assert body[0]["path"].endswith("hello.txt")
    assert os.path.exists(body[0]["path"])


def test_upload_multiple_files():
    files = [
        ("files", ("a.txt", io.BytesIO(b"aaa"), "text/plain")),
        ("files", ("b.txt", io.BytesIO(b"bbb"), "text/plain")),
    ]
    res = client.post("/api/upload", files=files)
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 2
    names = {r["name"] for r in body}
    assert names == {"a.txt", "b.txt"}


def test_upload_no_files_returns_empty():
    res = client.post("/api/upload", files=[])
    assert res.status_code == 200
    assert res.json() == []


def test_duplicate_filename_no_collision():
    data1 = {"files": ("dup.txt", io.BytesIO(b"v1"), "text/plain")}
    data2 = {"files": ("dup.txt", io.BytesIO(b"v2"), "text/plain")}
    r1 = client.post("/api/upload", files=data1).json()
    r2 = client.post("/api/upload", files=data2).json()
    assert r1[0]["path"] != r2[0]["path"]
