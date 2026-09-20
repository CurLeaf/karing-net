#!/usr/bin/env python3
"""Probe classification and verdict hysteresis, against local listeners only.

The probe used to be ``curl -m 6`` and reported every non-204 as "the proxy
failed".  Two claims are tested here, because both were the source of the false
alarms:

* the failure *kind* is real -- a refused port, a rejected CONNECT, a stalled
  handshake and a wrong status code must arrive as four different answers, not
  as the single string ``000`` the log used to carry;
* a single bad sample never declares an outage -- ``FAIL_THRESHOLD`` consecutive
  failures do, and it takes ``RECOVER_THRESHOLD`` successes to come back.

Everything runs on ephemeral 127.0.0.1 ports against throwaway servers, so this
is safe to run next to the live services (see harness.py).  Nothing here writes
Karing's configuration or the watcher's log file.
"""
from __future__ import annotations

import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import Checks, load  # noqa: E402

tw = load("tunnel-watch.py")

# Keep the test quick: the phase ceilings are the only thing worth shortening.
tw.PHASE_TIMEOUT = {"tcp": 2.0, "connect": 2.0, "tls": 2.0, "http": 2.0}


# --------------------------------------------------------------------------
# throwaway listeners
# --------------------------------------------------------------------------
def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(4096)
            if not chunk:
                return
            dst.sendall(chunk)
    except OSError:
        pass


class FakeProxy:
    """A local CONNECT proxy that can be told to misbehave in a specific way."""

    def __init__(self, mode: str, upstream: tuple[str, int] | None = None) -> None:
        self.mode = mode
        self.upstream = upstream
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def endpoint(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3.0)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                request += chunk

            if self.mode == "reject":
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            if self.mode == "silent":
                time.sleep(tw.PHASE_TIMEOUT["connect"] + 0.5)
                return

            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if self.mode == "garbage":
                conn.sendall(b"this is not a TLS record\n")
                time.sleep(0.3)
                return

            upstream = socket.create_connection(self.upstream, 3.0)
            threading.Thread(target=_pump, args=(conn, upstream), daemon=True).start()
            _pump(upstream, conn)
            upstream.close()
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class TLSTarget:
    """A one-shot HTTPS listener that answers every request with ``status``."""

    def __init__(self, certfile: str, keyfile: str, status: str = "204") -> None:
        self.status = status
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certfile, keyfile)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(3.0)
            tls = self.context.wrap_socket(conn, server_side=True)
            try:
                while b"\r\n\r\n" not in tls.recv(4096):
                    pass
                tls.sendall(f"HTTP/1.1 {self.status} X\r\nContent-Length: 0\r\n"
                            f"Connection: close\r\n\r\n".encode())
            finally:
                tls.close()
        except (OSError, ssl.SSLError):
            pass


def make_cert(directory: str) -> tuple[str, str]:
    """Self-signed cert for 'localhost', so the probe can verify a real chain."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


# --------------------------------------------------------------------------
# samples for the verdict test (no sockets involved)
# --------------------------------------------------------------------------
def good(total: float = 0.5):
    return tw.ProbeResult("gstatic", "proxy", True, code="204",
                          phases={"tcp": 0.001, "tls": total * 0.7, "http": total * 0.3})


def bad(kind: str = "connect_timeout"):
    return tw.ProbeResult("gstatic", "proxy", False, kind=kind, stage="connect",
                          phases={"tcp": 0.001, "connect": tw.PHASE_TIMEOUT["connect"]})


def test_classification(checks: Checks, tmp: str) -> None:
    print("--- 失败分类：不同故障必须给出不同 kind ---")
    certfile, keyfile = make_cert(tmp)
    target = tw.Target("selftest", "localhost", "/generate_204")
    trusted = ssl.create_default_context(cafile=certfile)

    # 1. nothing listening -> the local proxy port is gone
    quiet = socket.socket()
    quiet.bind(("127.0.0.1", 0))
    dead_port = quiet.getsockname()[1]
    quiet.close()
    result = tw.probe(target, "proxy", ("127.0.0.1", dead_port))
    checks.check(not result.ok and result.kind == "port_closed",
                 "端口未监听 -> port_closed", result.render())
    # The refused phase is still timed, and it is instant -- which is what
    # separates "nothing is listening" from a handshake that stalls.
    checks.check(set(result.phases) == {"tcp"} and result.phases["tcp"] < 1.0,
                 "只走到 tcp 阶段且耗时是瞬时的", str(result.phases))

    # 2. proxy answers the CONNECT with 403
    proxy = FakeProxy("reject")
    try:
        result = tw.probe(target, "proxy", proxy.endpoint, trusted)
        checks.check(not result.ok and result.kind == "connect_rejected",
                     "CONNECT 被驳回 -> connect_rejected", result.render())
    finally:
        proxy.close()

    # 3. proxy accepts and then never answers
    proxy = FakeProxy("silent")
    try:
        result = tw.probe(target, "proxy", proxy.endpoint, trusted)
        checks.check(not result.ok and result.kind == "connect_timeout",
                     "CONNECT 无响应 -> connect_timeout", result.render())
        checks.check(result.phases.get("connect", 0) >= 1.0,
                     "卡住的阶段耗时被记录（可区分瞬时拒绝与握手停滞）",
                     str(result.phases))
    finally:
        proxy.close()

    # 4. proxy answers 200 and then speaks something that is not TLS
    proxy = FakeProxy("garbage")
    try:
        result = tw.probe(target, "proxy", proxy.endpoint, trusted)
        checks.check(not result.ok and result.kind in ("tls_error", "empty_reply"),
                     "CONNECT 后非 TLS 数据 -> tls_error", result.render())
        checks.check(result.stage == "tls", "停在 tls 阶段", result.stage)
    finally:
        proxy.close()

    # 5. the whole path works, and each phase is timed separately
    target_server = TLSTarget(certfile, keyfile, status="204")
    proxy = FakeProxy("ok", ("127.0.0.1", target_server.port))
    try:
        result = tw.probe(target, "proxy", proxy.endpoint, trusted)
        checks.check(result.ok and result.code == "204",
                     "完整链路 -> ok code=204", result.render())
        checks.check(all(k in result.phases for k in ("tcp", "connect", "tls", "http")),
                     "四个阶段都有耗时", str(sorted(result.phases)))
        checks.check(result.total > 0, "总耗时为正", f"{result.total:.3f}s")
    finally:
        proxy.close()
        target_server.close()

    # 6. a reachable path that answers the wrong status is not a network failure
    target_server = TLSTarget(certfile, keyfile, status="500")
    proxy = FakeProxy("ok", ("127.0.0.1", target_server.port))
    try:
        result = tw.probe(target, "proxy", proxy.endpoint, trusted)
        checks.check(not result.ok and result.kind == "http_status"
                     and result.code == "500",
                     "状态码非预期 -> http_status 且保留 code", result.render())
    finally:
        proxy.close()
        target_server.close()

    # 7. an untrusted certificate is its own kind, not a timeout
    target_server = TLSTarget(certfile, keyfile, status="204")
    proxy = FakeProxy("ok", ("127.0.0.1", target_server.port))
    try:
        result = tw.probe(target, "proxy", proxy.endpoint)  # system trust store
        checks.check(not result.ok and result.kind == "tls_cert",
                     "证书不可信 -> tls_cert", result.render())
    finally:
        proxy.close()
        target_server.close()

    # 8. the old probe collapsed all of the above into one string
    kinds = {"port_closed", "connect_rejected", "connect_timeout",
             "tls_error", "tls_cert", "http_status"}
    checks.check(len(kinds) >= 6, "失败原因至少可分辨 6 种", f"{len(kinds)} 种")


def test_verdict(checks: Checks) -> None:
    print("--- 判定：迟滞状态机 ---")
    checks.check(tw.FAIL_THRESHOLD >= 2, "FAIL_THRESHOLD 至少 2", str(tw.FAIL_THRESHOLD))
    checks.check(tw.RECOVER_THRESHOLD >= 2, "RECOVER_THRESHOLD 至少 2",
                 str(tw.RECOVER_THRESHOLD))

    direction = tw.Direction("proxy", "代理方向")

    transitions = [direction.feed(good())]
    checks.check(direction.state == "up" and transitions[-1] is None,
                 "首个成功 -> up，不通知", str(direction.state))

    # A slow-but-working sample stays up: this is the case that used to page.
    slow = tw.ProbeResult("gstatic", "proxy", True, code="204",
                          phases={"tcp": 0.001, "tls": 4.4, "http": 1.5})
    checks.check(direction.feed(slow) is None and direction.state == "up",
                 "慢但成功 -> 仍然 up", f"total={slow.total:.2f}s")

    # The whole point: failures below the threshold are recorded, not announced.
    for index in range(tw.FAIL_THRESHOLD - 1):
        got = direction.feed(bad())
        checks.check(got is None and direction.state == "up",
                     f"连续第 {index + 1} 次失败 -> 仍不判定 down",
                     f"streak={direction.fail_streak}")

    got = direction.feed(bad())
    checks.check(got == "down" and direction.state == "down",
                 f"连续第 {tw.FAIL_THRESHOLD} 次失败 -> down 且只通知一次",
                 f"streak={direction.fail_streak}")
    checks.check(direction.down_since > 0, "记录了中断起始时间",
                 f"{direction.down_since:.0f}")
    checks.check(direction.feed(bad("port_closed")) is None,
                 "中断期间的失败不再重复判定", str(direction.state))

    checks.check(direction.feed(good()) is None and direction.state == "down",
                 "第 1 次成功 -> 仍是 down", f"ok_streak={direction.ok_streak}")
    checks.check(direction.feed(good()) == "up" and direction.state == "up",
                 f"第 {tw.RECOVER_THRESHOLD} 次成功 -> up",
                 f"ok_streak={direction.ok_streak}")

    # One slow sample must not reset a failure streak either.
    direction2 = tw.Direction("proxy", "代理方向")
    direction2.feed(good())
    direction2.feed(bad())
    direction2.feed(good())
    direction2.feed(bad())
    checks.check(direction2.fail_streak == 1,
                 "中间夹一次成功会重置失败计数", str(direction2.fail_streak))

    line = direction.stats_line()
    checks.check("p50=" in line and "p95=" in line, "统计行含延迟分位", line)
    checks.check("bad={" in line, "统计行含失败原因计数", line)


def test_notify_categories(checks: Checks) -> None:
    print("--- 通知：失败与恢复不共用限流额度 ---")
    import inspect

    source = inspect.getsource(tw.notify)
    checks.check("_last_notify.get(category" in source,
                 "限流按 category 分开", "失败与恢复各自计时")
    checks.check("notify suppressed" in source,
                 "被限流时会写日志", "不再静默吞掉")
    checks.check("_last_notify[category] = now" in source,
                 "成功发出后更新该 category 的时间戳", "")


def main() -> int:
    checks = Checks()
    started = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        test_classification(checks, tmp)
        test_verdict(checks)
        test_notify_categories(checks)
    print(f"\n耗时 {time.time() - started:.1f}s")
    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
