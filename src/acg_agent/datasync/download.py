"""下载数据包：超时、重试、断点续传、校验。只用 Python 自带的 urllib 和 hashlib。

下载先写到 <文件名>.part，校验通过后才改名成正式文件；中途断了，下次从 .part 的末尾接着下（HTTP Range）。
HTTPS 证书用 certifi 提供的证书包：python.org 安装的 Python 在 macOS 上不会自动使用系统证书，
直接用会 CERTIFICATE_VERIFY_FAILED（TS-047）。
"""

import base64
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import certifi

USER_AGENT = "acgn-agent/0.1 (local, non-commercial; https://github.com/haoran-ou/acgn-agent)"
TIMEOUT_S = 30
RETRIES = 3
RETRY_WAIT_S = 3.0
CHUNK = 1 << 20  # 1 MB

Progress = Callable[[int, int | None], None]  # (已下载字节, 总字节或 None)
_SSL = ssl.create_default_context(cafile=certifi.where())


def _open(req: urllib.request.Request):
    return urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL)


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class Checksum:
    algo: str  # "sha256" / "sha512"
    value: str  # 十六进制

    @classmethod
    def from_integrity(cls, integrity: str) -> "Checksum":
        """npm 的 integrity 字段：'sha512-<base64>'。"""
        algo, _, b64 = integrity.partition("-")
        return cls(algo, base64.b64decode(b64).hex())

    @classmethod
    def from_digest(cls, digest: str) -> "Checksum":
        """GitHub 的 digest 字段：'sha256:<hex>'。"""
        algo, _, value = digest.partition(":")
        return cls(algo, value.lower())


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _request(url: str, method: str = "GET", headers: dict | None = None) -> urllib.request.Request:
    if not url.startswith(("https://", "http://127.0.0.1:")):  # 127.0.0.1 只给测试用的本机服务器
        raise DownloadError(f"只从 https 地址下载：{url}")
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})


def _with_retries(action, what: str, retries: int = RETRIES, wait_s: float = RETRY_WAIT_S):
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return action()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise DownloadError(f"{what}失败：HTTP {e.code}") from e  # 4xx（除了限流）重试也没用
            last = e
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise DownloadError(f"{what}失败：HTTPS 证书校验失败（{e.reason.verify_message}）") from e  # 重试也没用
            last = e
        except (TimeoutError, ConnectionError) as e:
            last = e
        if attempt < retries - 1:
            time.sleep(wait_s * (attempt + 1))
    raise DownloadError(f"{what}失败（重试 {retries} 次）：{last}。请检查网络后重试") from last


def fetch_json(url: str) -> dict:
    def go():
        with _open(_request(url)) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return _with_retries(go, f"读取 {url} ")


def head(url: str) -> dict[str, str]:
    """只要响应头（文件大小、最后修改时间），不下载内容。"""
    def go():
        with _open(_request(url, "HEAD")) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}
    return _with_retries(go, f"查询 {url} ")


def download(url: str, dest: Path, checksum: Checksum | None = None, progress: Progress | None = None,
             retries: int = RETRIES, wait_s: float = RETRY_WAIT_S) -> Path:
    """下载到 dest。已经存在、而且校验通过的文件直接返回，不重复下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and checksum and file_hash(dest, checksum.algo) == checksum.value:
        return dest
    part = dest.with_name(dest.name + ".part")

    def go():
        done = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={done}-"} if done else {}
        with _open(_request(url, headers=headers)) as resp:
            if done and resp.status != 206:  # 服务器不支持断点续传：从头下
                done = 0
            length = resp.headers.get("Content-Length")
            total = done + int(length) if length else None
            with part.open("ab" if done else "wb") as f:
                while block := resp.read(CHUNK):
                    f.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
            if total is not None and done < total:
                raise ConnectionError(f"只下载了 {done}/{total} 字节")

    _with_retries(go, f"下载 {dest.name} ", retries, wait_s)
    if checksum:
        actual = file_hash(part, checksum.algo)
        if actual != checksum.value:
            part.unlink(missing_ok=True)  # 坏文件不留着，下次从头下
            raise DownloadError(f"{dest.name} 校验失败：期望 {checksum.algo} {checksum.value[:16]}…，实际 {actual[:16]}…")
    part.replace(dest)
    return dest
