#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "stack-supervisor.toml"


def load_env(env_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not env_path.exists():
        return env

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


@dataclass
class SupervisorConfig:
    default_profile: str
    poll_interval_seconds: int
    status_host: str
    status_port: int
    runtime_dir: Path
    profiles: dict[str, list[str]]
    memory_auto_start_soft_limit_gb: float
    memory_hard_limit_gb: float
    min_free_percent_for_conditional_start: float
    max_swap_used_gb: float
    heavy_model_budget_threshold_gb: float
    max_auto_heavy_models: int


@dataclass
class ServiceConfig:
    name: str
    kind: str
    command: list[str]
    cwd: Path
    port: int
    health_url: str
    health_headers: dict[str, str]
    startup_grace_seconds: int
    startup_timeout_seconds: int
    restart_backoff_seconds: int
    unhealthy_threshold: int
    stop_timeout_seconds: int
    watch_files: list[Path]
    memory_budget_gb: float
    on_demand: bool
    pinned: bool
    heavy_group: str | None = None


@dataclass
class ServiceRuntime:
    name: str
    desired: bool = False
    desired_reason: str = "stopped"
    status: str = "stopped"
    pid: int | None = None
    managed: bool = False
    adopted: bool = False
    healthy: bool = False
    health_failures: int = 0
    restart_count: int = 0
    last_error: str | None = None
    last_exit_code: int | None = None
    last_start_time: float | None = None
    last_healthy_time: float | None = None
    next_restart_time: float = 0.0
    startup_deadline: float = 0.0
    observed_command: str | None = None
    watch_fingerprint: dict[str, int] = field(default_factory=dict)
    blocked_reason: str | None = None
    log_path: str | None = None
    last_probe_time: float | None = None
    last_probe_ok: bool | None = None
    last_probe_summary: str | None = None
    last_probe_payload: dict[str, Any] | None = None
    last_auto_start_time: float | None = None
    last_used_time: float | None = None
    last_denied_reason: str | None = None
    eviction_protected: bool = False


def read_config(path: Path) -> tuple[SupervisorConfig, dict[str, ServiceConfig]]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    supervisor_raw = raw["supervisor"]
    memory_policy_raw = raw.get("memory_policy", {})
    runtime_dir = (path.parent / supervisor_raw.get("runtime_dir", "./runtime")).resolve()
    supervisor = SupervisorConfig(
        default_profile=supervisor_raw["default_profile"],
        poll_interval_seconds=int(supervisor_raw.get("poll_interval_seconds", 5)),
        status_host=supervisor_raw.get("status_host", "127.0.0.1"),
        status_port=int(supervisor_raw.get("status_port", 4060)),
        runtime_dir=runtime_dir,
        profiles={name: list(values) for name, values in raw.get("profiles", {}).items()},
        memory_auto_start_soft_limit_gb=float(memory_policy_raw.get("auto_start_soft_limit_gb", 88)),
        memory_hard_limit_gb=float(memory_policy_raw.get("hard_limit_gb", 96)),
        min_free_percent_for_conditional_start=float(memory_policy_raw.get("min_free_percent_for_conditional_start", 8)),
        max_swap_used_gb=float(memory_policy_raw.get("max_swap_used_gb", 8)),
        heavy_model_budget_threshold_gb=float(memory_policy_raw.get("heavy_model_budget_threshold_gb", 30)),
        max_auto_heavy_models=int(memory_policy_raw.get("max_auto_heavy_models", 2)),
    )

    services: dict[str, ServiceConfig] = {}
    for entry in raw.get("services", []):
        headers: dict[str, str] = {}
        for header in entry.get("health_headers", []):
            key, value = header.split(":", 1)
            headers[key.strip()] = value.strip()
        services[entry["name"]] = ServiceConfig(
            name=entry["name"],
            kind=entry.get("kind", "service"),
            command=list(entry["command"]),
            cwd=(path.parent / entry.get("cwd", ".")).resolve(),
            port=int(entry["port"]),
            health_url=entry["health_url"],
            health_headers=headers,
            startup_grace_seconds=int(entry.get("startup_grace_seconds", 30)),
            startup_timeout_seconds=int(entry.get("startup_timeout_seconds", 300)),
            restart_backoff_seconds=int(entry.get("restart_backoff_seconds", 10)),
            unhealthy_threshold=int(entry.get("unhealthy_threshold", 3)),
            stop_timeout_seconds=int(entry.get("stop_timeout_seconds", 20)),
            watch_files=[(path.parent / item).resolve() for item in entry.get("watch_files", [])],
            memory_budget_gb=float(entry.get("memory_budget_gb", 0)),
            on_demand=bool(entry.get("on_demand", False)),
            pinned=bool(entry.get("pinned", False)),
            heavy_group=entry.get("heavy_group"),
        )
    return supervisor, services


def now_ts() -> float:
    return time.time()


def is_pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def find_listener_pid(port: int) -> int | None:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def find_lock_holder_pid(lock_path: Path) -> int | None:
    result = subprocess.run(
        ["lsof", "-t", str(lock_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def read_lock_file_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if raw.isdigit():
        return int(raw)
    return None


def lock_file_is_stale(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    holder_pid = find_lock_holder_pid(lock_path)
    if holder_pid is not None and is_pid_alive(holder_pid):
        return False
    file_pid = read_lock_file_pid(lock_path)
    if file_pid is not None and is_pid_alive(file_pid):
        return False
    return True


def get_process_command(pid: int | None) -> str | None:
    if pid is None:
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def request_ok(url: str, headers: dict[str, str], timeout: float = 3.0) -> tuple[bool, str | None]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            if 200 <= status < 300:
                return True, None
            return False, f"http_status={status}"
    except urllib.error.HTTPError as exc:
        return False, f"http_status={exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
            return True, parsed, None
    except urllib.error.HTTPError as exc:
        try:
            parsed = json.loads(exc.read().decode("utf-8"))
            return False, parsed, f"http_status={exc.code}"
        except Exception:  # noqa: BLE001
            return False, None, f"http_status={exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)


def parse_memory_pressure_snapshot(output: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"raw": output}
    total_match = re.search(r"The system has (\d+)", output)
    if total_match:
        snapshot["total_bytes"] = int(total_match.group(1))

    free_match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
    if free_match:
        snapshot["free_percent"] = int(free_match.group(1))

    pageouts_match = re.search(r"Pageouts:\s*(\d+)", output)
    if pageouts_match:
        snapshot["pageouts"] = int(pageouts_match.group(1))

    swapins_match = re.search(r"Swapins:\s*(\d+)", output)
    if swapins_match:
        snapshot["swapins"] = int(swapins_match.group(1))

    swapouts_match = re.search(r"Swapouts:\s*(\d+)", output)
    if swapouts_match:
        snapshot["swapouts"] = int(swapouts_match.group(1))
    return snapshot


def get_swap_used_gb() -> float | None:
    result = subprocess.run(
        ["sysctl", "vm.swapusage"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    match = re.search(r"used = ([0-9.]+)([MG])", result.stdout)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "M":
        return round(value / 1024, 2)
    return round(value, 2)


def get_memory_snapshot() -> dict[str, Any]:
    result = subprocess.run(
        ["memory_pressure"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {
            "ok": False,
            "error": (result.stderr or result.stdout or "memory_pressure failed").strip(),
        }

    snapshot = parse_memory_pressure_snapshot(result.stdout)
    snapshot["ok"] = True
    snapshot["swap_used_gb"] = get_swap_used_gb()
    return snapshot


class StatusHandler(BaseHTTPRequestHandler):
    supervisor: "StackSupervisor"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path in {"/", "/ui"}:
            html_text = self.supervisor.render_dashboard()
            encoded = html_text.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        if parsed.path == "/status":
            payload = self.supervisor.build_status_payload()
            self._write_json(payload)
            return

        if parsed.path.startswith("/logs/"):
            service_name = parsed.path.split("/logs/", 1)[1]
            if not service_name:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            log_path = self.supervisor.log_path_for(service_name)
            if log_path is None or not log_path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            params = parse_qs(parsed.query)
            requested_lines = params.get("lines", ["200"])[0]
            try:
                line_count = max(20, min(1000, int(requested_lines)))
            except ValueError:
                line_count = 200
            text = tail_text(log_path, line_count)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:  # noqa: BLE001
            self.send_error(HTTPStatus.BAD_REQUEST)
            return

        if parsed.path == "/control":
            action = str(payload.get("action", "")).strip()
            service_name = str(payload.get("service", "")).strip()
            ok, response = self.supervisor.control_service(service_name, action)
        elif parsed.path == "/ensure-service":
            service_name = str(payload.get("service", "")).strip()
            timeout_seconds = float(payload.get("timeout_seconds", 60) or 60)
            ok, response = self.supervisor.ensure_service_ready(service_name, timeout_seconds)
        elif parsed.path == "/profile":
            profile_name = str(payload.get("profile", "")).strip()
            ok, response = self.supervisor.apply_profile(profile_name)
        elif parsed.path == "/probe":
            service_name = str(payload.get("service", "")).strip()
            ok, response = self.supervisor.probe_service(service_name)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        encoded = json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _write_json(self, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def tail_text(path: Path, line_count: int) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-line_count:])


class StackSupervisor:
    def __init__(
        self,
        supervisor_config: SupervisorConfig,
        services: dict[str, ServiceConfig],
        desired_services: set[str],
        env: dict[str, str],
        current_profile: str,
    ) -> None:
        self.config = supervisor_config
        self.services = services
        self.desired_services = desired_services
        self.env = env
        self.current_profile = current_profile
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.runtimes: dict[str, ServiceRuntime] = {
            name: ServiceRuntime(
                name=name,
                desired=(name in desired_services),
                desired_reason="profile" if name in desired_services else "stopped",
                eviction_protected=(name in desired_services),
            )
            for name in services
        }
        self.shutdown_requested = threading.Event()
        self.state_lock = threading.RLock()
        self.status_server: ThreadingHTTPServer | None = None
        self.status_thread: threading.Thread | None = None
        self.runtime_dir = self.config.runtime_dir
        self.logs_dir = self.runtime_dir / "logs"
        self.state_path = self.runtime_dir / "supervisor-status.json"
        self.lock_path = self.runtime_dir / "supervisor.lock"
        self.lock_handle = None
        self.ensure_locks: dict[str, threading.Lock] = {
            name: threading.Lock() for name in services
        }
        self.heavy_start_lock = threading.Lock()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def acquire_lock(self) -> None:
        if lock_file_is_stale(self.lock_path):
            self.lock_path.unlink(missing_ok=True)
        self.lock_handle = self.lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_handle.close()
            self.lock_handle = None
            if lock_file_is_stale(self.lock_path):
                self.lock_path.unlink(missing_ok=True)
                self.lock_handle = self.lock_path.open("w", encoding="utf-8")
                fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise RuntimeError(f"supervisor already running: {self.lock_path}") from None
        self.lock_handle.write(str(os.getpid()))
        self.lock_handle.flush()

    def release_lock(self) -> None:
        if self.lock_handle is None:
            return
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.lock_handle.close()
            self.lock_handle = None

    def log_path_for(self, service_name: str) -> Path | None:
        if service_name not in self.services:
            return None
        return self.logs_dir / f"{service_name}.log"

    def compute_watch_fingerprint(self, config: ServiceConfig) -> dict[str, int]:
        fingerprint: dict[str, int] = {}
        for path in config.watch_files:
            key = str(path)
            try:
                fingerprint[key] = path.stat().st_mtime_ns
            except FileNotFoundError:
                fingerprint[key] = -1
        return fingerprint

    def is_heavy_service(self, service_name: str) -> bool:
        config = self.services[service_name]
        if config.heavy_group:
            return True
        return config.memory_budget_gb >= self.config.heavy_model_budget_threshold_gb

    def mark_service_used(self, service_name: str, ts: float | None = None) -> None:
        runtime = self.runtimes[service_name]
        runtime.last_used_time = ts or now_ts()

    def set_desired_reason(self, service_name: str, reason: str) -> None:
        runtime = self.runtimes[service_name]
        runtime.desired_reason = reason
        runtime.eviction_protected = reason in {"profile", "manual"}

    def get_running_budget_gb_locked(self) -> float:
        total = 0.0
        for name, runtime in self.runtimes.items():
            if runtime.pid is not None:
                total += self.services[name].memory_budget_gb
        return round(total, 2)

    def count_running_heavy_services_locked(self, exclude: str | None = None) -> int:
        count = 0
        for name, runtime in self.runtimes.items():
            if name == exclude:
                continue
            if runtime.pid is None:
                continue
            if self.is_heavy_service(name):
                count += 1
        return count

    def get_memory_health(self) -> tuple[bool, str, dict[str, Any]]:
        snapshot = get_memory_snapshot()
        if not snapshot.get("ok"):
            return False, f"无法读取 memory_pressure：{snapshot.get('error', 'unknown error')}", snapshot

        free_percent = snapshot.get("free_percent")
        if isinstance(free_percent, int) and free_percent < self.config.min_free_percent_for_conditional_start:
            return (
                False,
                f"当前可用内存比例仅 {free_percent}%，低于条件准入阈值 {self.config.min_free_percent_for_conditional_start}%",
                snapshot,
            )

        swap_used_gb = snapshot.get("swap_used_gb")
        if isinstance(swap_used_gb, (float, int)) and swap_used_gb > self.config.max_swap_used_gb:
            return (
                False,
                f"当前 swap 已使用 {swap_used_gb}GB，高于条件准入阈值 {self.config.max_swap_used_gb}GB",
                snapshot,
            )

        return True, "当前内存压力满足条件准入", snapshot

    def build_admission_report_locked(self, service_name: str) -> dict[str, Any]:
        current_budget_gb = self.get_running_budget_gb_locked()
        target_budget_gb = self.services[service_name].memory_budget_gb
        projected_budget_gb = round(current_budget_gb + (0 if self.runtimes[service_name].pid else target_budget_gb), 2)
        current_heavy_count = self.count_running_heavy_services_locked(exclude=service_name if self.runtimes[service_name].pid else None)
        projected_heavy_count = current_heavy_count + (
            0 if self.runtimes[service_name].pid or not self.is_heavy_service(service_name) else 1
        )
        return {
            "service": service_name,
            "current_budget_gb": current_budget_gb,
            "target_budget_gb": target_budget_gb,
            "projected_budget_gb": projected_budget_gb,
            "soft_limit_gb": self.config.memory_auto_start_soft_limit_gb,
            "hard_limit_gb": self.config.memory_hard_limit_gb,
            "current_heavy_count": current_heavy_count,
            "projected_heavy_count": projected_heavy_count,
            "max_auto_heavy_models": self.config.max_auto_heavy_models,
        }

    def get_protected_running_services_locked(self, exclude: str | None = None) -> list[str]:
        protected: list[str] = []
        for name, runtime in self.runtimes.items():
            if name == exclude or runtime.pid is None or not runtime.eviction_protected:
                continue
            protected.append(name)
        protected.sort()
        return protected

    def _eviction_candidates_locked(self, target_service: str) -> list[str]:
        candidates: list[str] = []
        for name, runtime in self.runtimes.items():
            if name == target_service or runtime.pid is None:
                continue
            config = self.services[name]
            if not config.on_demand or config.pinned or runtime.eviction_protected:
                continue
            candidates.append(name)

        target_is_heavy = self.is_heavy_service(target_service)
        target_group = self.services[target_service].heavy_group

        def sort_key(name: str) -> tuple[int, float, float]:
            config = self.services[name]
            runtime = self.runtimes[name]
            same_heavy_group = int(not (target_is_heavy and self.is_heavy_service(name)))
            exact_group_penalty = int(not (target_group and config.heavy_group == target_group))
            last_used = runtime.last_used_time or 0.0
            return (same_heavy_group + exact_group_penalty, last_used, -config.memory_budget_gb)

        candidates.sort(key=sort_key)
        return candidates

    def explain_admission_failure_locked(
        self,
        service_name: str,
        admission: dict[str, Any],
        evicted_services: list[str],
    ) -> str:
        runtime = self.runtimes[service_name]
        protected_running = self.get_protected_running_services_locked(exclude=service_name if runtime.pid else None)
        protected_suffix = ""
        if protected_running:
            protected_suffix = f"；当前受保护服务：{', '.join(protected_running)}"
        if evicted_services:
            protected_suffix += f"；已自动腾退：{', '.join(evicted_services)}"

        if admission["projected_heavy_count"] > admission["max_auto_heavy_models"]:
            return (
                f"自动拉起 {service_name} 后，重型模型并存数将达到 {admission['projected_heavy_count']}，"
                f"超过上限 {admission['max_auto_heavy_models']}，已拒绝本次自动拉起"
                f"{protected_suffix}"
            )

        if admission["projected_budget_gb"] > admission["hard_limit_gb"]:
            return (
                f"自动拉起 {service_name} 后，预计总预算 {admission['projected_budget_gb']}GB，"
                f"超过硬上限 {admission['hard_limit_gb']}GB，已拒绝本次自动拉起"
                f"{protected_suffix}"
            )

        return (
            f"自动拉起 {service_name} 未通过准入检查"
            f"{protected_suffix}"
        )

    def try_evict_for_service_locked(self, target_service: str) -> list[str]:
        stopped: list[str] = []
        while True:
            report = self.build_admission_report_locked(target_service)
            over_soft = report["projected_budget_gb"] > report["soft_limit_gb"]
            over_hard = report["projected_budget_gb"] > report["hard_limit_gb"]
            over_heavy = report["projected_heavy_count"] > report["max_auto_heavy_models"]
            if not (over_soft or over_hard or over_heavy):
                break

            candidates = self._eviction_candidates_locked(target_service)
            if not candidates:
                break
            victim = candidates[0]
            self.stop_service(victim, f"evicted for {target_service}")
            self.desired_services.discard(victim)
            self.runtimes[victim].desired = False
            self.set_desired_reason(victim, "stopped")
            stopped.append(victim)
        return stopped

    def build_status_payload(self) -> dict[str, Any]:
        with self.state_lock:
            memory_snapshot = get_memory_snapshot()
            items: list[dict[str, Any]] = []
            for name in sorted(self.runtimes):
                runtime = self.runtimes[name]
                service = self.services[name]
                item = asdict(runtime)
                item["kind"] = service.kind
                item["port"] = service.port
                item["health_url"] = service.health_url
                item["command"] = service.command
                item["memory_budget_gb"] = service.memory_budget_gb
                item["on_demand"] = service.on_demand
                item["pinned"] = service.pinned
                item["heavy_group"] = service.heavy_group
                items.append(item)
            return {
                "updated_at": int(now_ts()),
                "desired_services": sorted(self.desired_services),
                "current_profile": self.current_profile,
                "profiles": self.config.profiles,
                "status_host": self.config.status_host,
                "status_port": self.config.status_port,
                "gateway_base_url": self.env.get("LOCAL_GATEWAY_BASE_URL", "http://127.0.0.1:4000/v1"),
                "gateway_api_key": self.env.get("LITELLM_MASTER_KEY", ""),
                "llama_api_key": self.env.get("LOCAL_LLAMA_API_KEY", ""),
                "memory_policy": {
                    "soft_limit_gb": self.config.memory_auto_start_soft_limit_gb,
                    "hard_limit_gb": self.config.memory_hard_limit_gb,
                    "min_free_percent_for_conditional_start": self.config.min_free_percent_for_conditional_start,
                    "max_swap_used_gb": self.config.max_swap_used_gb,
                    "heavy_model_budget_threshold_gb": self.config.heavy_model_budget_threshold_gb,
                    "max_auto_heavy_models": self.config.max_auto_heavy_models,
                },
                "memory_snapshot": memory_snapshot,
                "running_budget_gb": self.get_running_budget_gb_locked(),
                "services": items,
            }

    def control_service(self, service_name: str, action: str) -> tuple[bool, dict[str, Any]]:
        if service_name not in self.services:
            return False, {"ok": False, "error": f"unknown service: {service_name}"}
        if action not in {"start", "stop", "restart"}:
            return False, {"ok": False, "error": f"unsupported action: {action}"}

        with self.state_lock:
            runtime = self.runtimes[service_name]
            self.current_profile = "custom"

            if action == "start":
                self.desired_services.add(service_name)
                runtime.desired = True
                self.set_desired_reason(service_name, "manual")
                runtime.blocked_reason = None
                runtime.next_restart_time = 0.0
                if runtime.pid is None:
                    self.ensure_started(service_name, now_ts())

            elif action == "stop":
                self.desired_services.discard(service_name)
                runtime.desired = False
                self.set_desired_reason(service_name, "stopped")
                runtime.blocked_reason = None
                runtime.next_restart_time = 0.0
                if runtime.pid is not None:
                    self.stop_service(service_name, "stopped via dashboard")
                runtime.next_restart_time = 0.0
                runtime.status = "stopped"
                runtime.healthy = False

            elif action == "restart":
                self.desired_services.add(service_name)
                runtime.desired = True
                self.set_desired_reason(service_name, "manual")
                runtime.blocked_reason = None
                runtime.next_restart_time = 0.0
                if runtime.pid is not None:
                    self.stop_service(service_name, "restarted via dashboard")
                    runtime.next_restart_time = 0.0
                self.ensure_started(service_name, now_ts())

            self.write_status()
            return True, {
                "ok": True,
                "service": service_name,
                "action": action,
                "status": self.build_status_payload(),
            }

    def ensure_service_ready(self, service_name: str, timeout_seconds: float) -> tuple[bool, dict[str, Any]]:
        if service_name not in self.services:
            return False, {"ok": False, "error": f"unknown service: {service_name}"}

        service_lock = self.ensure_locks[service_name]
        heavy_lock = self.heavy_start_lock if self.is_heavy_service(service_name) else None
        timeout_seconds = max(5.0, min(timeout_seconds, 1800.0))

        if heavy_lock is not None:
            heavy_lock.acquire()
        service_lock.acquire()
        try:
            with self.state_lock:
                runtime = self.runtimes[service_name]
                service = self.services[service_name]
                self.mark_service_used(service_name)

                if runtime.healthy:
                    runtime.last_denied_reason = None
                    self.write_status()
                    return True, {
                        "ok": True,
                        "ready": True,
                        "service": service_name,
                        "reason": "already_healthy",
                        "status": self.build_status_payload(),
                    }

                evicted_services = self.try_evict_for_service_locked(service_name)
                admission = self.build_admission_report_locked(service_name)

                if admission["projected_heavy_count"] > admission["max_auto_heavy_models"]:
                    reason = self.explain_admission_failure_locked(service_name, admission, evicted_services)
                    runtime.last_denied_reason = reason
                    self.write_status()
                    return False, {
                        "ok": False,
                        "error": reason,
                        "admission": admission,
                        "evicted_services": evicted_services,
                        "status": self.build_status_payload(),
                    }

                if admission["projected_budget_gb"] > admission["hard_limit_gb"]:
                    reason = self.explain_admission_failure_locked(service_name, admission, evicted_services)
                    runtime.last_denied_reason = reason
                    self.write_status()
                    return False, {
                        "ok": False,
                        "error": reason,
                        "admission": admission,
                        "evicted_services": evicted_services,
                        "status": self.build_status_payload(),
                    }

                memory_health: dict[str, Any] | None = None
                if admission["projected_budget_gb"] > admission["soft_limit_gb"]:
                    healthy, health_reason, memory_health = self.get_memory_health()
                    if not healthy:
                        runtime.last_denied_reason = health_reason
                        self.write_status()
                        return False, {
                            "ok": False,
                            "error": health_reason,
                            "admission": admission,
                            "memory_snapshot": memory_health,
                            "evicted_services": evicted_services,
                            "status": self.build_status_payload(),
                        }

                self.desired_services.add(service_name)
                runtime.desired = True
                self.set_desired_reason(service_name, "auto")
                runtime.last_denied_reason = None
                runtime.last_auto_start_time = now_ts()
                runtime.blocked_reason = None
                runtime.next_restart_time = 0.0
                if runtime.pid is None:
                    self.ensure_started(service_name, now_ts())
                self.write_status()

            deadline = time.time() + timeout_seconds
            last_error = ""
            while time.time() < deadline:
                with self.state_lock:
                    runtime = self.runtimes[service_name]
                    service = self.services[service_name]
                    self.refresh_process_state(service_name)

                    if runtime.pid is None and time.time() >= runtime.next_restart_time:
                        self.ensure_started(service_name, now_ts())

                    if runtime.pid is not None:
                        ok, error = request_ok(service.health_url, service.health_headers, timeout=2.0)
                        if ok:
                            runtime.status = "healthy"
                            runtime.healthy = True
                            runtime.health_failures = 0
                            runtime.last_error = None
                            runtime.blocked_reason = None
                            runtime.last_healthy_time = now_ts()
                            self.mark_service_used(service_name)
                            self.write_status()
                            return True, {
                                "ok": True,
                                "ready": True,
                                "service": service_name,
                                "admission": admission,
                                "evicted_services": evicted_services,
                                "memory_snapshot": memory_health,
                                "status": self.build_status_payload(),
                            }

                        last_error = error or "health check failed"
                        runtime.healthy = False
                        runtime.last_error = last_error
                        if runtime.last_start_time is None:
                            runtime.last_start_time = now_ts()
                        if time.time() < runtime.last_start_time + service.startup_grace_seconds:
                            runtime.status = "starting"
                        else:
                            runtime.status = "degraded"
                    self.write_status()
                time.sleep(0.5)

            with self.state_lock:
                runtime = self.runtimes[service_name]
                runtime.last_denied_reason = last_error or "startup timeout"
                self.write_status()
            return False, {
                "ok": False,
                "error": f"服务 {service_name} 在 {int(timeout_seconds)} 秒内未就绪",
                "last_error": last_error or "startup timeout",
                "status": self.build_status_payload(),
            }
        finally:
            service_lock.release()
            if heavy_lock is not None:
                heavy_lock.release()

    def record_probe_result(
        self,
        service_name: str,
        ok: bool,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        with self.state_lock:
            runtime = self.runtimes[service_name]
            runtime.last_probe_time = now_ts()
            runtime.last_probe_ok = ok
            runtime.last_probe_summary = summary
            runtime.last_probe_payload = payload
            self.write_status()

    def apply_profile(self, profile_name: str) -> tuple[bool, dict[str, Any]]:
        if profile_name not in self.config.profiles:
            return False, {"ok": False, "error": f"unknown profile: {profile_name}"}

        with self.state_lock:
            new_desired = set(self.config.profiles[profile_name])
            self.current_profile = profile_name
            self.desired_services = set(new_desired)

            for name, runtime in self.runtimes.items():
                runtime.desired = name in new_desired
                self.set_desired_reason(name, "profile" if name in new_desired else "stopped")
                runtime.blocked_reason = None

            for name in self.services:
                runtime = self.runtimes[name]
                if name not in new_desired:
                    runtime.next_restart_time = 0.0
                    if runtime.pid is not None:
                        self.stop_service(name, f"disabled by profile {profile_name}")
                    runtime.next_restart_time = 0.0
                    runtime.status = "stopped"
                    runtime.healthy = False
                elif runtime.pid is None:
                    runtime.next_restart_time = 0.0
                    self.ensure_started(name, now_ts())

            self.write_status()
            return True, {
                "ok": True,
                "profile": profile_name,
                "status": self.build_status_payload(),
            }

    def probe_service(self, service_name: str) -> tuple[bool, dict[str, Any]]:
        if service_name not in self.services:
            return False, {"ok": False, "error": f"unknown service: {service_name}"}

        service = self.services[service_name]
        runtime = self.runtimes[service_name]
        if service.kind == "gateway":
            return False, {"ok": False, "error": "gateway service has no model probe"}

        gateway_base = self.env.get("LOCAL_GATEWAY_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.env.get('LITELLM_MASTER_KEY', '')}",
        }

        if service_name == "embed-m3":
            ok, parsed, error = request_json(
                "POST",
                f"{gateway_base}/embeddings",
                headers=headers,
                payload={"model": "embed-m3", "input": "probe"},
                timeout=20.0,
            )
            if not ok:
                details = {
                    "error": error,
                    "details": parsed,
                }
                self.record_probe_result(service_name, False, f"embedding probe failed: {error}", details)
                return False, {
                    "ok": False,
                    "error": error,
                    "details": parsed,
                    "status": self.build_status_payload(),
                }
            dim = len((((parsed or {}).get("data") or [{}])[0]).get("embedding") or [])
            response = {
                "ok": True,
                "service": service_name,
                "probe_type": "embedding",
                "dimension": dim,
                "status": runtime.status,
            }
            self.record_probe_result(
                service_name,
                True,
                f"向量接口验证成功，返回维度 {dim}",
                {
                    "probe_type": "embedding",
                    "dimension": dim,
                },
            )
            with self.state_lock:
                self.mark_service_used(service_name)
            response["status"] = self.build_status_payload()
            return True, response

        ok, parsed, error = request_json(
            "POST",
            f"{gateway_base}/chat/completions",
            headers=headers,
            payload={
                "model": service_name,
                "messages": [{"role": "user", "content": "只回复：probe ok"}],
                "stream": False,
                "max_tokens": 32,
            },
            timeout=45.0,
        )
        if not ok:
            details = {
                "error": error,
                "details": parsed,
            }
            self.record_probe_result(service_name, False, f"对话接口验证失败：{error}", details)
            return False, {
                "ok": False,
                "error": error,
                "details": parsed,
                "status": self.build_status_payload(),
            }

        content = (
            ((((parsed or {}).get("choices") or [{}])[0]).get("message") or {}).get("content")
        )
        response = {
            "ok": True,
            "service": service_name,
            "probe_type": "chat",
            "response_preview": content,
            "status": runtime.status,
        }
        self.record_probe_result(
            service_name,
            True,
            f"对话接口验证成功：{(content or '').strip() or '模型已响应，但返回空文本'}",
            {
                "probe_type": "chat",
                "response_preview": content,
            },
        )
        with self.state_lock:
            self.mark_service_used(service_name)
        response["status"] = self.build_status_payload()
        return True, response

    def render_dashboard(self) -> str:
        gateway_url = html.escape(self.env.get("LOCAL_GATEWAY_BASE_URL", "http://127.0.0.1:4000/v1"))
        current_profile = html.escape(self.current_profile or self.config.default_profile)
        stack_dir = html.escape(str(ROOT_DIR))
        start_command = html.escape(f"cd {ROOT_DIR} && ./start_stack_supervisor.sh --profile {self.current_profile or self.config.default_profile}")
        stop_command = html.escape(f'pkill -f "stack_supervisor.py run --profile {self.current_profile or self.config.default_profile}"')
        restart_command = html.escape(
            f'pkill -f "stack_supervisor.py run --profile {self.current_profile or self.config.default_profile}" && cd {ROOT_DIR} && ./start_stack_supervisor.sh --profile {self.current_profile or self.config.default_profile}'
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local LLM Stack Console</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f1ea;
      --panel: #fffdf8;
      --ink: #1d1b18;
      --muted: #6f675e;
      --line: #ddd4c8;
      --ok: #1a7f37;
      --warn: #a15c00;
      --bad: #b42318;
      --accent: #0f5c7a;
      --accent-soft: #d9edf5;
      --mono: "SFMono-Regular", "Menlo", monospace;
      --sans: "SF Pro Display", "PingFang SC", "Helvetica Neue", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #efe4cf 0, transparent 28rem),
        linear-gradient(180deg, #f8f4ec 0%, var(--bg) 100%);
    }}
    .shell {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 28px 22px 40px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .panel {{
      background: color-mix(in srgb, var(--panel) 92%, white 8%);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 18px 48px rgba(59, 45, 20, 0.08);
    }}
    .hero-main {{
      padding: 22px;
    }}
    .hero h1 {{
      margin: 0 0 6px;
      font-size: 32px;
      letter-spacing: -0.04em;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
    }}
    .command-card {{
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255,255,255,0.72);
      padding: 10px;
      display: grid;
      gap: 6px;
    }}
    .command-card h2 {{
      margin: 0;
      font-size: 14px;
      letter-spacing: -0.02em;
    }}
    .command-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }}
    .command-item {{
      display: grid;
      gap: 2px;
      align-content: start;
    }}
    .command-item strong {{
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      line-height: 1.1;
    }}
    .inline-code {{
      margin: 0;
      min-height: 0;
      max-height: none;
      padding: 5px 7px;
      border-radius: 8px;
      background: #1e1c19;
      color: #f0efe9;
      font-family: var(--mono);
      font-size: 11px;
      line-height: 1.15;
      overflow-x: auto;
      white-space: nowrap;
      word-break: normal;
    }}
    .meta {{
      display: grid;
      gap: 12px;
      padding: 22px;
    }}
    .meta-row {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.65);
    }}
    .meta-row strong {{
      display: block;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 4px;
    }}
    .meta-row code {{
      font-family: var(--mono);
      font-size: 13px;
      word-break: break-all;
    }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin: 16px 0 14px;
    }}
    .toolbar-left {{
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    .toolbar-right {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .button {{
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 600;
    }}
    .button:hover {{
      border-color: #c6b8a7;
      background: #fff9ef;
    }}
    .button:disabled {{
      cursor: not-allowed;
      opacity: 0.45;
    }}
    .button-small {{
      padding: 6px 10px;
      font-size: 12px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    .status-healthy {{ background: #dbf5df; color: var(--ok); }}
    .status-starting, .status-adopted, .status-degraded {{ background: #fff1d5; color: var(--warn); }}
    .status-unhealthy, .status-blocked, .status-backoff {{ background: #fde7e7; color: var(--bad); }}
    .status-stopped {{ background: #ebe6df; color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(360px, 0.8fr);
      gap: 18px;
    }}
    .services {{
      padding: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      text-align: left;
      vertical-align: top;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .service-name {{
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .service-kind {{
      color: var(--muted);
      font-size: 12px;
    }}
    .mono {{
      font-family: var(--mono);
      font-size: 12px;
      word-break: break-all;
      color: #40382f;
    }}
    .muted {{
      color: var(--muted);
      font-size: 12px;
    }}
    .metric {{
      display: grid;
      gap: 4px;
      margin-top: 6px;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 12px;
    }}
    .metric-value {{
      font-size: 13px;
      color: var(--ink);
      line-height: 1.45;
      word-break: break-word;
    }}
    .feedback {{
      min-height: 18px;
      margin-bottom: 14px;
      color: var(--accent);
      font-size: 13px;
    }}
    .logs {{
      padding: 18px;
      display: grid;
      gap: 12px;
    }}
    .side-stack {{
      display: grid;
      gap: 18px;
    }}
    .insights {{
      padding: 18px;
      display: grid;
      gap: 14px;
    }}
    .insight-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255,255,255,0.68);
      padding: 14px;
    }}
    .insight-card h2 {{
      margin: 0 0 10px;
      font-size: 15px;
      letter-spacing: -0.02em;
    }}
    .chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 10px;
      background: #f1eadc;
      border: 1px solid #ddcfbb;
      font-size: 12px;
      font-weight: 600;
      color: #4a4035;
    }}
    .kv {{
      display: grid;
      grid-template-columns: 96px minmax(0, 1fr);
      gap: 8px 10px;
      align-items: start;
      font-size: 13px;
      margin-bottom: 10px;
    }}
    .kv strong {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .probe-good {{
      color: var(--ok);
      font-weight: 700;
    }}
    .probe-bad {{
      color: var(--bad);
      font-weight: 700;
    }}
    .mini-pre {{
      min-height: 0;
      max-height: 220px;
      padding: 12px;
      border-radius: 12px;
      background: #1e1c19;
      color: #f0efe9;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.5;
      border: 1px solid rgba(255,255,255,0.08);
      white-space: pre-wrap;
    }}
    .log-toolbar {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    select {{
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: white;
      font: inherit;
    }}
    pre {{
      margin: 0;
      min-height: 420px;
      max-height: 72vh;
      overflow: auto;
      padding: 16px;
      border-radius: 14px;
      background: #1e1c19;
      color: #f0efe9;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.5;
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .foot {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 1100px) {{
      .hero, .grid {{
        grid-template-columns: 1fr;
      }}
      .command-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="panel hero-main">
        <h1>Local LLM Stack Console</h1>
        <p>查看当前哪些模型正在运行、哪些已停止、每个服务的监听端口、健康状态、重启次数、日志与统一调用入口。这个页面由 supervisor 直接提供，不依赖额外前端服务。</p>
        <p style="margin-top:10px;">说明：显示为“常驻保护”的服务不会被自动腾退；显示为“自动按需”的服务会在需要时被拉起，也可能为了给其他模型腾内存而被自动停止。</p>
        <div class="command-card">
          <h2>4060 控制台启停命令</h2>
          <div class="muted">当前目录：{stack_dir}；当前 profile：{current_profile}</div>
          <div class="command-grid">
            <div class="command-item">
              <strong>启动</strong>
              <pre class="inline-code">{start_command}</pre>
            </div>
            <div class="command-item">
              <strong>停止</strong>
              <pre class="inline-code">{stop_command}</pre>
            </div>
            <div class="command-item">
              <strong>重启</strong>
              <pre class="inline-code">{restart_command}</pre>
            </div>
          </div>
        </div>
      </div>
      <div class="panel meta">
        <div class="meta-row">
          <strong>统一调用地址</strong>
          <code>{gateway_url}</code>
        </div>
        <div class="meta-row">
          <strong>状态查询地址</strong>
          <code>http://{self.config.status_host}:{self.config.status_port}/status</code>
        </div>
        <div class="meta-row">
          <strong>日志目录</strong>
          <code>{html.escape(str(self.logs_dir))}</code>
        </div>
      </div>
    </section>

    <div class="toolbar">
      <div class="toolbar-left">
        <button class="button" onclick="refreshAll()">刷新状态</button>
        <span id="updatedAt" class="muted">正在加载...</span>
      </div>
      <div class="toolbar-right">
        <span class="muted">当前 profile:</span>
        <select id="profileSelect"></select>
        <button class="button" onclick="applyProfile()">应用 profile</button>
        <div class="muted">自动刷新: 5 秒</div>
      </div>
    </div>
    <div id="feedback" class="feedback"></div>

    <section class="grid">
      <div class="panel services">
        <table>
          <thead>
            <tr>
              <th>服务信息</th>
              <th>运行情况</th>
              <th>进程情况</th>
              <th>接口说明</th>
              <th>操作</th>
              <th>连通性验证</th>
              <th>问题说明</th>
            </tr>
          </thead>
          <tbody id="serviceRows"></tbody>
        </table>
      </div>

      <div class="side-stack">
        <div class="panel insights">
          <div class="insight-card">
            <h2>当前 Profile</h2>
            <div id="profileSummary" class="kv"></div>
            <div id="profileMembers" class="chip-list"></div>
          </div>
          <div class="insight-card">
            <h2>最近 Probe</h2>
            <div id="probeSummary" class="kv"></div>
            <pre id="probePayload" class="mini-pre">尚未执行 probe。</pre>
          </div>
          <div class="insight-card">
            <h2>统一 API 调用信息</h2>
            <div id="apiSummary" class="kv"></div>
            <pre id="apiExample" class="mini-pre">选择一个服务后，这里会显示统一 URL 和对应模型名的调用示例。</pre>
          </div>
        </div>

        <div class="panel logs">
          <div class="log-toolbar">
            <select id="serviceSelect" onchange="refreshLogs(); renderPanels()"></select>
            <select id="lineCount" onchange="refreshLogs()">
              <option value="120">最近 120 行</option>
              <option value="240" selected>最近 240 行</option>
              <option value="500">最近 500 行</option>
            </select>
            <button class="button" onclick="refreshLogs()">刷新日志</button>
          </div>
          <pre id="logContent">正在加载日志...</pre>
          <div class="foot">如果某个服务显示为 unhealthy、blocked 或 backoff，先看这里的最近日志，再看状态页中的 last_error 和最近 probe 结果。</div>
        </div>
      </div>
    </section>
  </div>

  <script>
    let latest = null;
    let controlBusy = false;

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"]/g, (ch) => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}})[ch]);
    }}

    function fmtTs(value) {{
      if (!value) return "-";
      const millis = value > 1000000000000 ? value : value * 1000;
      return new Date(millis).toLocaleString("zh-CN", {{ hour12: false }});
    }}

    function boolText(value) {{
      return value ? "是" : "否";
    }}

    function healthText(value) {{
      return value ? "已通过" : "未通过";
    }}

    function desiredReasonText(value) {{
      const mapping = {{
        profile: "预设常驻",
        manual: "手工常驻",
        auto: "自动按需",
        stopped: "当前未纳入运行计划",
      }};
      return mapping[value] || value || "-";
    }}

    function evictionPolicyText(service) {{
      if (service.pinned || service.eviction_protected) {{
        return "不会自动腾退";
      }}
      if (service.on_demand) {{
        return "必要时允许自动腾退";
      }}
      return "按当前配置不会自动腾退";
    }}

    function statusText(value) {{
      const mapping = {{
        healthy: "运行正常",
        starting: "正在启动",
        adopted: "已接管现有进程",
        degraded: "部分异常",
        unhealthy: "健康检查失败",
        blocked: "已阻塞",
        backoff: "退避后重试",
        stopped: "未运行",
      }};
      return mapping[value] || value || "-";
    }}

    function metric(label, value) {{
      return `
        <div class="metric">
          <div class="metric-label">${{escapeHtml(label)}}</div>
          <div class="metric-value">${{escapeHtml(value)}}</div>
        </div>
      `;
    }}

    function renderKv(targetId, pairs) {{
      const target = document.getElementById(targetId);
      target.innerHTML = "";
      for (const [label, value] of pairs) {{
        const key = document.createElement("strong");
        key.textContent = label;
        const val = document.createElement("div");
        if (value instanceof HTMLElement) {{
          val.appendChild(value);
        }} else {{
          val.textContent = String(value ?? "-");
        }}
        target.appendChild(key);
        target.appendChild(val);
      }}
    }}

    function getSelectedService() {{
      const selected = document.getElementById("serviceSelect").value;
      return (latest?.services || []).find((item) => item.name === selected) || null;
    }}

    function probeLabel(service) {{
      if (service.last_probe_ok === true) return "成功";
      if (service.last_probe_ok === false) return "失败";
      return "未测试";
    }}

    function renderProfilePanel(payload) {{
      const currentProfile = payload.current_profile || "custom";
      const desired = payload.desired_services || [];
      const healthyDesired = (payload.services || []).filter((service) => service.desired && service.healthy).length;
      const latestDenied = (payload.services || [])
        .filter((service) => service.last_denied_reason)
        .sort((a, b) => (b.last_auto_start_time || b.last_used_time || 0) - (a.last_auto_start_time || a.last_used_time || 0))[0];
      renderKv("profileSummary", [
        ["名称", currentProfile],
        ["目标服务", `计划运行 ${{desired.length}} 个服务`],
        ["健康情况", `其中 ${{healthyDesired}} 个已经健康可用`],
        ["统一 URL", payload.gateway_base_url || "-"],
        ["当前预算", `${{payload.running_budget_gb ?? "-"}} GB / 软阈值 ${{payload.memory_policy?.soft_limit_gb ?? "-"}} GB / 硬阈值 ${{payload.memory_policy?.hard_limit_gb ?? "-"}} GB`],
        ["最近拒绝", latestDenied ? latestDenied.last_denied_reason : "最近没有准入拒绝"],
      ]);

      const holder = document.getElementById("profileMembers");
      holder.innerHTML = "";
      for (const name of desired) {{
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = name;
        holder.appendChild(chip);
      }}
      if (!desired.length) {{
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = "当前没有期望运行的服务";
        holder.appendChild(chip);
      }}
    }}

    function renderProbePanel(service) {{
      if (!service) {{
        renderKv("probeSummary", [
          ["服务", "未选择"],
          ["结果", "未测试"],
          ["时间", "-"],
          ["摘要", "-"],
        ]);
        document.getElementById("probePayload").textContent = "尚未执行 probe。";
        return;
      }}

      const statusNode = document.createElement("span");
      statusNode.className = service.last_probe_ok ? "probe-good" : (service.last_probe_ok === false ? "probe-bad" : "");
      statusNode.textContent = probeLabel(service);
      renderKv("probeSummary", [
        ["服务", service.name],
        ["结果", statusNode],
        ["时间", fmtTs(service.last_probe_time)],
        ["摘要", service.last_probe_summary || "-"],
      ]);
      document.getElementById("probePayload").textContent = service.last_probe_payload
        ? JSON.stringify(service.last_probe_payload, null, 2)
        : "尚未执行 probe。";
    }}

    function renderApiPanel(service) {{
      const baseUrl = latest?.gateway_base_url || "http://127.0.0.1:4000/v1";
      const apiKey = latest?.gateway_api_key || "sk-local-gateway";
      if (!service) {{
        renderKv("apiSummary", [
          ["服务", "未选择"],
          ["建议接口", "-"],
          ["模型名", "-"],
          ["下游端口", "-"],
        ]);
        document.getElementById("apiExample").textContent = "选择一个服务后，这里会显示统一 URL 和对应模型名的调用示例。";
        return;
      }}

      let endpoint = "/v1/chat/completions";
      let example = [
        `curl ${{baseUrl}}/chat/completions`,
        `  -H 'Authorization: Bearer ${{apiKey}}'`,
        "  -H 'Content-Type: application/json'",
        `  -d '${{JSON.stringify({{ model: service.name, messages: [{{ role: "user", content: "你好" }}] }}, null, 2)}}'`,
      ].join("\\n");

      if (service.kind === "gateway") {{
        endpoint = "/v1/models";
        example = [
          `curl ${{baseUrl}}/models`,
          `  -H 'Authorization: Bearer ${{apiKey}}'`,
        ].join("\\n");
      }} else if (service.name === "embed-m3") {{
        endpoint = "/v1/embeddings";
        example = [
          `curl ${{baseUrl}}/embeddings`,
          `  -H 'Authorization: Bearer ${{apiKey}}'`,
          "  -H 'Content-Type: application/json'",
          `  -d '${{JSON.stringify({{ model: service.name, input: "probe" }}, null, 2)}}'`,
        ].join("\\n");
      }} else {{
        example += "\\n\\n";
        example += [
          `curl ${{baseUrl}}/responses`,
          `  -H 'Authorization: Bearer ${{apiKey}}'`,
          "  -H 'Content-Type: application/json'",
          `  -d '${{JSON.stringify({{ model: service.name, input: "你好" }}, null, 2)}}'`,
        ].join("\\n");
      }}

      renderKv("apiSummary", [
        ["服务", service.name],
        ["建议接口", `优先调用 ${{endpoint}}`],
        ["模型名", service.kind === "gateway" ? "网关自身" : service.name],
        ["下游端口", `最终转发到本机端口 ${{service.port ?? "-"}}`],
      ]);
      document.getElementById("apiExample").textContent = example;
    }}

    function renderPanels() {{
      if (!latest) return;
      renderProfilePanel(latest);
      const selectedService = getSelectedService();
      renderProbePanel(selectedService);
      renderApiPanel(selectedService);
    }}

    function renderServices(payload) {{
      const rows = document.getElementById("serviceRows");
      rows.innerHTML = "";
      const select = document.getElementById("serviceSelect");
      const previous = select.value;
      select.innerHTML = "";
      const profileSelect = document.getElementById("profileSelect");
      const previousProfile = profileSelect.value;
      profileSelect.innerHTML = "";

      const profiles = payload.profiles || {{}};
      for (const name of Object.keys(profiles)) {{
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        profileSelect.appendChild(option);
      }}
      if (payload.current_profile && profiles[payload.current_profile]) {{
        profileSelect.value = payload.current_profile;
      }} else if (previousProfile && profiles[previousProfile]) {{
        profileSelect.value = previousProfile;
      }}

      for (const service of payload.services) {{
        const tr = document.createElement("tr");
        const statusClass = "status-" + service.status;
        const command = Array.isArray(service.command) ? service.command.join(" ") : "";
        const probeSummary = service.last_probe_summary || "还没有执行过统一接口验证";
        const issueSummary = service.last_denied_reason
          ? `最近准入拒绝：${{service.last_denied_reason}}`
          : (service.last_error
          ? `最近错误：${{service.last_error}}`
          : (service.blocked_reason ? `当前限制：${{service.blocked_reason}}` : "当前没有记录到异常"));
        tr.innerHTML = `
          <td>
            <div class="service-name">${{escapeHtml(service.name)}}</div>
            <div class="service-kind">类型：${{escapeHtml(service.kind)}}</div>
            ${{metric("资源属性", `预估占用 ${{service.memory_budget_gb ?? "-"}} GB；按需加载：${{boolText(service.on_demand)}}；常驻保护：${{boolText(service.eviction_protected)}}`)}}
            ${{metric("装载方式", `当前属于：${{desiredReasonText(service.desired_reason)}}；自动腾退策略：${{evictionPolicyText(service)}}`)}}
          </td>
          <td>
            <span class="badge ${{statusClass}}">${{escapeHtml(statusText(service.status))}}</span>
            ${{metric("健康检查", `最近健康探测：${{healthText(service.healthy)}}`)}}
            ${{metric("自动恢复", `累计自动重启 ${{service.restart_count}} 次`)}}
            ${{metric("网关验证", `统一接口测试结果：${{probeLabel(service)}}`)}}
          </td>
          <td>
            ${{metric("进程编号", service.pid ?? "当前没有进程")}}
            ${{metric("管理方式", `supervisor 管理：${{boolText(service.managed)}}；接管现有进程：${{boolText(service.adopted)}}`)}}
            ${{metric("最近健康时间", fmtTs(service.last_healthy_time))}}
            ${{metric("最近自动拉起", fmtTs(service.last_auto_start_time))}}
          </td>
          <td>
            ${{metric("监听端口", `本地服务端口：${{service.port}}`)}}
            ${{metric("健康检查地址", service.health_url)}}
            ${{metric("启动命令", command || "未配置")}}
          </td>
          <td>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
              <button class="button button-small" ${{controlBusy ? "disabled" : ""}} onclick="controlService('${{escapeHtml(service.name)}}', 'start')">启动</button>
              <button class="button button-small" ${{controlBusy ? "disabled" : ""}} onclick="controlService('${{escapeHtml(service.name)}}', 'stop')">停止</button>
              <button class="button button-small" ${{controlBusy ? "disabled" : ""}} onclick="controlService('${{escapeHtml(service.name)}}', 'restart')">重启</button>
            </div>
            <div class="muted">用于直接控制这个服务进程。</div>
          </td>
          <td>
            <button class="button button-small" ${{(controlBusy || service.kind === 'gateway') ? "disabled" : ""}} onclick="probeService('${{escapeHtml(service.name)}}')">网关测试</button>
            <div class="muted">最近验证时间：${{escapeHtml(fmtTs(service.last_probe_time))}}</div>
          </td>
          <td>
            <div class="mono">${{escapeHtml(issueSummary)}}</div>
            <div class="muted">网关验证说明：${{escapeHtml(probeSummary)}}</div>
          </td>
        `;
        rows.appendChild(tr);

        const option = document.createElement("option");
        option.value = service.name;
        option.textContent = service.name;
        select.appendChild(option);
      }}

      if (previous && payload.services.some((item) => item.name === previous)) {{
        select.value = previous;
      }} else if (payload.services.length > 0) {{
        select.value = payload.services[0].name;
      }}
      renderPanels();
    }}

    async function applyProfile() {{
      if (controlBusy) return;
      const profile = document.getElementById("profileSelect").value;
      if (!profile) return;
      controlBusy = true;
      document.getElementById("feedback").textContent = `正在切换 profile: ${{profile}}`;
      renderServices(latest ?? {{ services: [], profiles: {{}} }});
      try {{
        const response = await fetch("/profile", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ profile }})
        }});
        const payload = await response.json();
        if (payload.status) {{
          latest = payload.status;
        }}
        if (!response.ok || !payload.ok) {{
          document.getElementById("feedback").textContent = "切换失败: " + (payload.error || response.status);
        }} else {{
          document.getElementById("feedback").textContent = `已切换到 profile: ${{profile}}`;
          renderServices(latest);
          refreshLogs();
        }}
      }} catch (error) {{
        document.getElementById("feedback").textContent = "切换失败: " + error;
      }} finally {{
        controlBusy = false;
        renderServices(latest ?? {{ services: [], profiles: {{}} }});
      }}
    }}

    async function controlService(service, action) {{
      if (controlBusy) return;
      controlBusy = true;
      document.getElementById("feedback").textContent = `正在执行: ${{service}} -> ${{action}}`;
      renderServices(latest ?? {{ services: [], profiles: {{}} }});
      try {{
        const response = await fetch("/control", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ service, action }})
        }});
        const payload = await response.json();
        if (payload.status) {{
          latest = payload.status;
        }}
        if (!response.ok || !payload.ok) {{
          document.getElementById("feedback").textContent = "控制失败: " + (payload.error || response.status);
        }} else {{
          document.getElementById("feedback").textContent = `操作成功: ${{service}} -> ${{action}}`;
          renderServices(latest);
          refreshLogs();
        }}
      }} catch (error) {{
        document.getElementById("feedback").textContent = "控制失败: " + error;
      }} finally {{
        controlBusy = false;
        renderServices(latest ?? {{ services: [], profiles: {{}} }});
      }}
    }}

    async function probeService(service) {{
      if (controlBusy) return;
      controlBusy = true;
      document.getElementById("feedback").textContent = `正在测试模型: ${{service}}`;
      renderServices(latest ?? {{ services: [], profiles: {{}} }});
      try {{
        const response = await fetch("/probe", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ service }})
        }});
        const payload = await response.json();
        if (payload.status) {{
          latest = payload.status;
        }}
        if (!response.ok || !payload.ok) {{
          document.getElementById("feedback").textContent = "测试失败: " + (payload.error || response.status);
        }} else if (payload.probe_type === "embedding") {{
          document.getElementById("feedback").textContent = `测试成功: ${{service}} embeddings dimension=${{payload.dimension}}`;
        }} else {{
          document.getElementById("feedback").textContent = `测试成功: ${{service}} -> ${{payload.response_preview ?? ""}}`;
        }}
        renderServices(latest ?? {{ services: [], profiles: {{}} }});
      }} catch (error) {{
        document.getElementById("feedback").textContent = "测试失败: " + error;
      }} finally {{
        controlBusy = false;
        renderServices(latest ?? {{ services: [], profiles: {{}} }});
      }}
    }}

    async function refreshLogs() {{
      const service = document.getElementById("serviceSelect").value;
      const lines = document.getElementById("lineCount").value;
      const logView = document.getElementById("logContent");
      if (!service) {{
        logView.textContent = "没有可查看的服务日志。";
        return;
      }}
      try {{
        const response = await fetch(`/logs/${{encodeURIComponent(service)}}?lines=${{encodeURIComponent(lines)}}`, {{ cache: "no-store" }});
        if (!response.ok) {{
          logView.textContent = `日志读取失败: HTTP ${{response.status}}`;
          return;
        }}
        const text = await response.text();
        logView.textContent = text || "暂无日志输出。";
      }} catch (error) {{
        logView.textContent = "日志读取失败: " + error;
      }}
    }}

    async function refreshAll() {{
      try {{
        const response = await fetch("/status", {{ cache: "no-store" }});
        latest = await response.json();
        renderServices(latest);
        document.getElementById("updatedAt").textContent = "最近更新: " + fmtTs(latest.updated_at);
        refreshLogs();
      }} catch (error) {{
        document.getElementById("updatedAt").textContent = "状态读取失败: " + error;
      }}
    }}

    refreshAll();
    setInterval(refreshAll, 5000);
  </script>
</body>
</html>
"""

    def write_status(self) -> None:
        payload = self.build_status_payload()
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def start_status_server(self) -> None:
        handler_cls = type("SupervisorStatusHandler", (StatusHandler,), {})
        handler_cls.supervisor = self
        self.status_server = ThreadingHTTPServer((self.config.status_host, self.config.status_port), handler_cls)
        self.status_thread = threading.Thread(target=self.status_server.serve_forever, daemon=True)
        self.status_thread.start()

    def stop_status_server(self) -> None:
        if self.status_server is not None:
            self.status_server.shutdown()
            self.status_server.server_close()
            self.status_server = None
        if self.status_thread is not None:
            self.status_thread.join(timeout=2)
            self.status_thread = None

    def handle_signal(self, signum: int, _frame: Any) -> None:
        self.shutdown_requested.set()
        for runtime in self.runtimes.values():
            runtime.last_error = f"received signal {signum}"

    def run(self) -> int:
        self.acquire_lock()
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)
        self.start_status_server()
        try:
            while not self.shutdown_requested.is_set():
                with self.state_lock:
                    self.monitor_once()
                    self.write_status()
                self.shutdown_requested.wait(self.config.poll_interval_seconds)
        finally:
            with self.state_lock:
                self.stop_all_managed()
                self.write_status()
            self.stop_status_server()
            self.release_lock()
        return 0

    def stop_all_managed(self) -> None:
        for name in self.services:
            runtime = self.runtimes[name]
            if runtime.managed and runtime.pid is not None:
                self.stop_service(name, "supervisor_shutdown")

    def monitor_once(self) -> None:
        current_time = now_ts()
        for name in self.services:
            runtime = self.runtimes[name]
            config = self.services[name]
            runtime.desired = name in self.desired_services
            runtime.log_path = str(self.log_path_for(name))

            if not runtime.desired:
                if runtime.managed and runtime.pid is not None:
                    self.stop_service(name, "not_desired")
                else:
                    runtime.status = "stopped"
                    runtime.healthy = False
                continue

            self.refresh_process_state(name)

            if runtime.pid is not None:
                self.handle_config_change(name)
                self.check_health(name, current_time)
                continue

            if current_time < runtime.next_restart_time:
                runtime.status = "backoff"
                runtime.healthy = False
                continue

            self.ensure_started(name, current_time)

    def refresh_process_state(self, name: str) -> None:
        runtime = self.runtimes[name]
        config = self.services[name]
        process = self.processes.get(name)

        if process is not None:
            exit_code = process.poll()
            if exit_code is None:
                runtime.pid = process.pid
                runtime.managed = True
                runtime.adopted = False
                runtime.observed_command = " ".join(config.command)
                return

            runtime.last_exit_code = exit_code
            runtime.last_error = f"process exited with code {exit_code}"
            runtime.pid = None
            runtime.managed = False
            runtime.healthy = False
            runtime.health_failures = 0
            runtime.status = "backoff"
            runtime.next_restart_time = now_ts() + config.restart_backoff_seconds
            self.processes.pop(name, None)
            return

        if is_port_open(config.port):
            pid = find_listener_pid(config.port)
            if pid is not None and is_pid_alive(pid):
                runtime.pid = pid
                runtime.managed = False
                runtime.adopted = True
                if runtime.desired_reason == "stopped":
                    self.set_desired_reason(name, "manual")
                runtime.observed_command = get_process_command(pid)
                return

        runtime.pid = None
        runtime.managed = False
        runtime.adopted = False
        runtime.observed_command = None

    def handle_config_change(self, name: str) -> None:
        runtime = self.runtimes[name]
        config = self.services[name]
        current = self.compute_watch_fingerprint(config)
        if not runtime.watch_fingerprint:
            runtime.watch_fingerprint = current
            return
        if current == runtime.watch_fingerprint:
            return
        runtime.last_error = "watched files changed, restarting"
        self.stop_service(name, "config_changed")
        runtime.watch_fingerprint = current

    def check_health(self, name: str, current_time: float) -> None:
        runtime = self.runtimes[name]
        config = self.services[name]
        if runtime.last_start_time is None:
            runtime.last_start_time = current_time
        if runtime.startup_deadline == 0.0:
            runtime.startup_deadline = runtime.last_start_time + config.startup_timeout_seconds

        ok, error = request_ok(config.health_url, config.health_headers)
        if ok:
            runtime.status = "healthy"
            runtime.healthy = True
            runtime.health_failures = 0
            runtime.blocked_reason = None
            runtime.last_error = None
            runtime.last_healthy_time = current_time
            return

        runtime.health_failures += 1
        runtime.healthy = False
        runtime.last_error = error

        if current_time < (runtime.last_start_time + config.startup_grace_seconds):
            runtime.status = "starting"
            return

        if current_time >= runtime.startup_deadline:
            runtime.status = "unhealthy"
            self.restart_or_block(name, f"startup timeout: {error}")
            return

        if runtime.health_failures < config.unhealthy_threshold:
            runtime.status = "degraded"
            return

        runtime.status = "unhealthy"
        self.restart_or_block(name, error or "health check failed")

    def restart_or_block(self, name: str, reason: str) -> None:
        runtime = self.runtimes[name]
        if runtime.pid is None:
            runtime.blocked_reason = reason
            return

        if runtime.managed or self.command_looks_managed(name, runtime.observed_command):
            self.stop_service(name, reason)
            return

        runtime.blocked_reason = reason
        runtime.status = "blocked"

    def ensure_started(self, name: str, current_time: float) -> None:
        runtime = self.runtimes[name]
        config = self.services[name]
        pid = find_listener_pid(config.port)
        if pid is not None:
            runtime.pid = pid
            runtime.adopted = True
            runtime.managed = False
            runtime.status = "adopted"
            if runtime.desired_reason == "stopped":
                self.set_desired_reason(name, "manual")
            runtime.observed_command = get_process_command(pid)
            runtime.watch_fingerprint = self.compute_watch_fingerprint(config)
            return

        log_path = self.log_path_for(name)
        assert log_path is not None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting {name}\n")
            log_handle.flush()
            process = subprocess.Popen(  # noqa: S603
                config.command,
                cwd=str(config.cwd),
                env=self.env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )

        self.processes[name] = process
        runtime.pid = process.pid
        runtime.managed = True
        runtime.adopted = False
        runtime.status = "starting"
        runtime.healthy = False
        runtime.health_failures = 0
        runtime.restart_count += 1
        runtime.last_start_time = current_time
        runtime.startup_deadline = current_time + config.startup_timeout_seconds
        runtime.next_restart_time = 0.0
        runtime.blocked_reason = None
        runtime.watch_fingerprint = self.compute_watch_fingerprint(config)
        runtime.observed_command = " ".join(config.command)

    def stop_service(self, name: str, reason: str) -> None:
        runtime = self.runtimes[name]
        config = self.services[name]
        pid = runtime.pid
        if pid is None:
            return

        runtime.last_error = reason
        runtime.status = "backoff"
        runtime.healthy = False
        runtime.health_failures = 0
        runtime.next_restart_time = now_ts() + config.restart_backoff_seconds

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        deadline = time.time() + config.stop_timeout_seconds
        while time.time() < deadline and is_pid_alive(pid):
            time.sleep(0.2)

        if is_pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except PermissionError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        runtime.pid = None
        runtime.managed = False
        runtime.adopted = False
        self.processes.pop(name, None)

    def command_looks_managed(self, name: str, observed_command: str | None) -> bool:
        if not observed_command:
            return False
        expected = self.services[name].command[0]
        expected_base = Path(expected).name
        return expected_base in observed_command or name in observed_command


def load_status(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def print_status(path: Path, as_json: bool) -> int:
    payload = load_status(path)
    if payload is None:
        print("no supervisor status file found", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"updated_at: {payload.get('updated_at')}")
    print(f"status_api: http://{payload.get('status_host')}:{payload.get('status_port')}/status")
    print("services:")
    for item in payload.get("services", []):
        print(
            f"  - {item['name']}: {item['status']} "
            f"(healthy={item['healthy']}, pid={item['pid']}, port={item['port']})"
        )
        if item.get("last_error"):
            print(f"    last_error: {item['last_error']}")
        if item.get("blocked_reason"):
            print(f"    blocked_reason: {item['blocked_reason']}")
    return 0


def resolve_desired_services(
    service_map: dict[str, ServiceConfig],
    supervisor_config: SupervisorConfig,
    profile: str | None,
    explicit_services: list[str],
) -> tuple[set[str], str]:
    desired: list[str] = []
    selected_profile = profile or supervisor_config.default_profile
    if profile:
        if profile not in supervisor_config.profiles:
            raise ValueError(f"unknown profile: {profile}")
        desired.extend(supervisor_config.profiles[profile])
    if explicit_services:
        desired.extend(explicit_services)
        selected_profile = "custom"
    if not desired:
        desired.extend(supervisor_config.profiles[supervisor_config.default_profile])

    unknown = sorted(set(desired) - set(service_map))
    if unknown:
        raise ValueError(f"unknown services: {', '.join(unknown)}")
    return set(desired), selected_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local LLM stack supervisor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run supervisor loop")
    run_parser.add_argument("--profile", help="service profile from stack-supervisor.toml")
    run_parser.add_argument("--service", action="append", default=[], help="add an individual service")

    status_parser = subparsers.add_parser("status", help="show latest supervisor status")
    status_parser.add_argument("--json", action="store_true", help="print raw json")

    subparsers.add_parser("profiles", help="list available profiles")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    supervisor_config, services = read_config(CONFIG_PATH)

    if args.command == "profiles":
        for name, members in supervisor_config.profiles.items():
            print(f"{name}: {', '.join(members)}")
        return 0

    if args.command == "status":
        return print_status(supervisor_config.runtime_dir / "supervisor-status.json", args.json)

    desired_services, current_profile = resolve_desired_services(
        services,
        supervisor_config,
        args.profile,
        args.service,
    )
    env = load_env(ROOT_DIR / ".env")
    supervisor = StackSupervisor(supervisor_config, services, desired_services, env, current_profile)
    try:
        return supervisor.run()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
