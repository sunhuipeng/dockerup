#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务管理 —— 飞牛 fnOS 应用后端服务

只用 Python 标准库实现，无第三方依赖。
提供静态页面托管 + JSON API，用于扫描 Compose 项目并执行 down / pull / up -d 等操作。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

APP_VERSION = "1.0.6"

# 与原始 shell 脚本保持一致的 compose 文件探测顺序
COMPOSE_CANDIDATES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]

DEFAULT_BASE_PATH = "/vol1/1000/docker"
MAX_TASK_LINES = 4000
MAX_TASKS_KEPT = 40

# docker 报错：The container name "/xxx" is already in use by container "abc..."
CONFLICT_RE = re.compile(
    r'container name\s*"?/?(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)"?\s*is already in use by container',
    re.I,
)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_cmd(args, cwd=None, timeout=30, env=None):
    """执行命令，返回 (returncode, stdout, stderr)。不经过 shell，避免注入。"""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "命令执行超时"
    except FileNotFoundError:
        return 127, "", "命令不存在: %s" % args[0]
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


class Settings(object):
    """保存在 TRIM_PKGVAR 下的简单配置。"""

    ALLOWED = ("base_path", "confirm_stop")

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "settings.json")
        self.lock = threading.Lock()
        self.data = {"base_path": DEFAULT_BASE_PATH, "confirm_stop": True}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for key in self.ALLOWED:
                    if key in raw:
                        self.data[key] = raw[key]
        except Exception:
            pass

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except Exception as exc:  # noqa: BLE001
                print("[warn] 保存设置失败: %s" % exc)

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def set(self, key, value):
        if key not in self.ALLOWED:
            return False
        with self.lock:
            self.data[key] = value
            val = dict(self.data)
        self.save()
        return val


class DockerEnv(object):
    """探测 docker / docker compose 可用性。"""

    def __init__(self):
        self.lock = threading.Lock()
        self._compose_prefix = False  # False = 未探测, None = 不可用, list = 命令前缀
        self._version = ""
        self._server_version = ""

    def detect(self, force=False):
        with self.lock:
            if self._compose_prefix is not False and not force:
                return
            self._compose_prefix = None
            self._version = ""
            self._server_version = ""

            docker_bin = shutil.which("docker") or "/usr/bin/docker"
            if not os.path.exists(docker_bin) and not shutil.which("docker"):
                return

            rc, out, err = run_cmd([docker_bin, "version", "--format", "{{.Server.Version}}"], timeout=15)
            if rc == 0:
                self._server_version = out.strip()

            rc, out, err = run_cmd([docker_bin, "compose", "version", "--short"], timeout=15)
            if rc == 0:
                self._compose_prefix = [docker_bin, "compose"]
                self._version = out.strip()
                return

            alt = shutil.which("docker-compose")
            if alt:
                rc, out, err = run_cmd([alt, "version", "--short"], timeout=15)
                if rc == 0:
                    self._compose_prefix = [alt]
                    self._version = out.strip()

    @property
    def compose_prefix(self):
        if self._compose_prefix is False:
            self.detect()
        return self._compose_prefix

    def info(self):
        self.detect()
        prefix = self._compose_prefix
        return {
            "ok": bool(prefix),
            "compose_command": " ".join(prefix) if prefix else "",
            "compose_version": self._version,
            "docker_version": self._server_version,
            "socket_ok": os.path.exists("/var/run/docker.sock"),
        }

    def command(self, args):
        prefix = self.compose_prefix
        if not prefix:
            return None
        return list(prefix) + list(args)


# ---------------------------------------------------------------------------
# 项目扫描
# ---------------------------------------------------------------------------

def find_compose_file(project_dir):
    for name in COMPOSE_CANDIDATES:
        candidate = os.path.join(project_dir, name)
        if os.path.isfile(candidate):
            return candidate, name
    return None, None


def resolve_project(base_path, name):
    """把前端传进来的项目名安全地解析为目录路径，禁止越权访问。"""
    if not name or not isinstance(name, str):
        return None, "项目名无效"
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        return None, "项目名无效"

    base_real = os.path.realpath(base_path)
    target = os.path.join(base_real, name)
    target_real = os.path.realpath(target)

    if os.path.dirname(target_real) != base_real:
        return None, "项目路径不在根目录下"
    if not os.path.isdir(target_real):
        return None, "项目目录不存在"
    return target_real, None


def parse_json_ps(text):
    """尝试把 docker compose ps --format json 的输出解析为容器列表，失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if text.startswith("["):
            data = json.loads(text)
        else:
            data = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    data.append(json.loads(line))
        if not isinstance(data, list):
            return None
        result = []
        for item in data:
            if not isinstance(item, dict):
                return None
            result.append(
                {
                    "name": item.get("Name") or item.get("name") or "",
                    "service": item.get("Service") or item.get("service") or "",
                    "state": (item.get("State") or item.get("state") or "").lower(),
                    "status": item.get("Status") or item.get("status") or "",
                    "image": item.get("Image") or item.get("image") or "",
                }
            )
        return result
    except Exception:
        return None


def parse_ps_output(text):
    """解析以 | 分隔的 ps 输出。"""
    containers = []
    for line in (text or "").splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        service = parts[1].strip()
        state = parts[2].strip().lower()
        status = parts[3].strip() if len(parts) > 3 else ""
        image = parts[4].strip() if len(parts) > 4 else ""
        if not name or not state:
            continue
        containers.append(
            {"name": name, "service": service, "state": state, "status": status, "image": image}
        )
    return containers


class ProjectScanner(object):
    def __init__(self, settings, docker_env):
        self.settings = settings
        self.docker = docker_env

    def project_state(self, project_dir, compose_path, use_cache=True):
        """查询项目内所有容器的状态。"""
        if not self.docker.compose_prefix:
            return {"ok": False, "error": "docker compose 不可用", "containers": [], "running": 0, "total": 0}

        # 优先使用 JSON 格式，老版本 compose 不支持时回退到 | 分隔模板
        cmd = self.docker.command(["-f", compose_path, "ps", "-a", "--format", "json"])
        rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=25)
        containers = parse_json_ps(out) if rc == 0 else None

        if containers is None:
            cmd = self.docker.command(
                ["-f", compose_path, "ps", "-a", "--format", "{{.Name}}|{{.Service}}|{{.State}}|{{.Status}}|{{.Image}}"]
            )
            rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=25)
            if rc != 0:
                return {
                    "ok": False,
                    "error": (err or "查询状态失败").splitlines()[0][:200],
                    "containers": [],
                    "running": 0,
                    "total": 0,
                }
            containers = parse_ps_output(out)
        running = sum(1 for c in containers if c.get("state") == "running")

        # compose ps 为空，但配置里声明了 container_name —— 可能有同名容器是外部创建的
        # （docker run / 旧项目 / 飞牛 Docker 管理器），此时也要把真实状态显示出来
        if not containers:
            extra = self._unmanaged_containers(project_dir, compose_path)
            if extra:
                running = sum(1 for c in extra if c.get("state") == "running")
                return {
                    "ok": True,
                    "error": "",
                    "containers": extra,
                    "running": running,
                    "total": len(extra),
                    "unmanaged": True,
                }

        return {"ok": True, "error": "", "containers": containers, "running": running, "total": len(containers)}

    def _declared_container_names(self, project_dir, compose_path):
        """从 compose 配置里取出显式声明的 container_name 列表。"""
        cmd = self.docker.command(["-f", compose_path, "config", "--format", "json"])
        rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=20)
        if rc != 0 or not out.strip():
            return []
        try:
            cfg = json.loads(out)
        except Exception:  # noqa: BLE001
            return []
        names = []
        for service in (cfg.get("services") or {}).values():
            if isinstance(service, dict) and service.get("container_name"):
                names.append(str(service["container_name"]))
        return names

    def _unmanaged_containers(self, project_dir, compose_path):
        """查询配置中声明的同名容器（不属于当前 compose 项目）。"""
        names = self._declared_container_names(project_dir, compose_path)
        if not names:
            return []
        rc, out, err = run_cmd(
            ["docker", "inspect", "--format", "{{.Name}}|{{.State.Status}}|{{.Config.Image}}"] + names,
            timeout=20,
        )
        result = []
        for line in (out or "").splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            name = parts[0].strip().lstrip("/")
            state = parts[1].strip().lower()
            if not name or not state:
                continue
            result.append(
                {
                    "name": name,
                    "service": name,
                    "state": state,
                    "status": "",
                    "image": parts[2].strip() if len(parts) > 2 else "",
                    "unmanaged": True,
                }
            )
        return result

    def scan(self):
        base_path = self.settings.get("base_path") or DEFAULT_BASE_PATH
        result = {
            "base_path": base_path,
            "exists": os.path.isdir(base_path),
            "readable": os.access(base_path, os.R_OK) if os.path.isdir(base_path) else False,
            "projects": [],
            "error": "",
        }

        if not result["exists"]:
            result["error"] = "根目录不存在，请检查路径或在设置中修改"
            return result
        if not result["readable"]:
            result["error"] = "根目录无读取权限"
            return result

        try:
            entries = sorted(os.listdir(base_path))
        except Exception as exc:  # noqa: BLE001
            result["error"] = "读取目录失败: %s" % exc
            return result

        projects = []
        for name in entries:
            if name.startswith("."):
                continue
            project_dir = os.path.join(base_path, name)
            if not os.path.isdir(project_dir):
                continue

            compose_path, compose_name = find_compose_file(project_dir)
            item = {
                "name": name,
                "path": project_dir,
                "has_compose": bool(compose_path),
                "compose_file": compose_name or "",
                "compose_path": compose_path or "",
                "containers": [],
                "running": 0,
                "total": 0,
                "unmanaged": False,
                "state_ok": False,
                "state_error": "",
                "mtime": 0,
            }

            try:
                item["mtime"] = int(os.path.getmtime(project_dir))
            except Exception:
                pass

            if compose_path:
                state = self.project_state(project_dir, compose_path)
                item["containers"] = state["containers"]
                item["running"] = state["running"]
                item["total"] = state["total"]
                item["unmanaged"] = state.get("unmanaged", False)
                item["state_ok"] = state["ok"]
                item["state_error"] = state["error"]

            projects.append(item)

        result["projects"] = projects
        return result


# ---------------------------------------------------------------------------
# 任务系统（后台执行 docker compose，日志可按偏移增量拉取）
# ---------------------------------------------------------------------------

ACTION_STEPS = {
    "upgrade": ["down", "pull", "up"],
    "pull": ["pull"],
    "up": ["up"],
    "down": ["down"],
    "restart": ["restart"],
}

ACTION_LABEL = {
    "upgrade": "升级",
    "pull": "拉取镜像",
    "up": "启动",
    "down": "停止",
    "restart": "重启",
}


class Task(object):
    def __init__(self, title, project="", action=""):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.project = project
        self.action = action
        self.lines = []
        self.running = True
        self.success = False
        self.exit_code = None
        self.started_at = time.time()
        self.finished_at = None
        self.lock = threading.Lock()

    def add(self, text, kind="out"):
        if text is None:
            return
        for raw in str(text).replace("\r\n", "\n").split("\n"):
            raw = raw.rstrip("\r")
            if raw == "":
                continue
            with self.lock:
                self.lines.append({"t": raw, "k": kind, "ts": now_str()})
                if len(self.lines) > MAX_TASK_LINES:
                    del self.lines[: len(self.lines) - MAX_TASK_LINES]

    def finish(self, success, code):
        with self.lock:
            self.running = False
            self.success = success
            self.exit_code = code
            self.finished_at = time.time()

    def snapshot(self, offset=0, limit=2000):
        with self.lock:
            total = len(self.lines)
            start = max(0, int(offset))
            chunk = self.lines[start : start + limit]
            return {
                "id": self.id,
                "title": self.title,
                "project": self.project,
                "action": self.action,
                "running": self.running,
                "success": self.success,
                "exit_code": self.exit_code,
                "total": total,
                "offset": start,
                "next": start + len(chunk),
                "lines": chunk,
                "elapsed": int((self.finished_at or time.time()) - self.started_at),
            }


class TaskManager(object):
    def __init__(self, settings, docker_env):
        self.settings = settings
        self.docker = docker_env
        self.tasks = {}
        self.order = []
        self.lock = threading.Lock()
        self.running_dirs = set()

    def get(self, task_id):
        with self.lock:
            return self.tasks.get(task_id)

    def latest(self):
        with self.lock:
            if not self.order:
                return None
            return self.tasks.get(self.order[-1])

    def _claim(self, project_dir):
        """标记项目为「执行中」，防止同一项目并发操作。"""
        with self.lock:
            if project_dir in self.running_dirs:
                return False
            self.running_dirs.add(project_dir)
            return True

    def _release(self, project_dir):
        with self.lock:
            self.running_dirs.discard(project_dir)

    def _register(self, task):
        with self.lock:
            self.tasks[task.id] = task
            self.order.append(task.id)
            while len(self.order) > MAX_TASKS_KEPT:
                old = self.order.pop(0)
                self.tasks.pop(old, None)

    def start(self, project_name, action):
        if action not in ACTION_STEPS:
            return None, "未知操作"

        base_path = self.settings.get("base_path")
        project_dir, err = resolve_project(base_path, project_name)
        if err:
            return None, err

        compose_path, compose_name = find_compose_file(project_dir)
        if not compose_path:
            return None, "该项目目录下找不到 compose 配置文件"

        if not self.docker.compose_prefix:
            return None, "docker compose 不可用，请确认 Docker 服务已安装并运行"

        if not self._claim(project_dir):
            return None, "该项目正在执行其他操作，请稍候"

        task = Task(
            "%s · %s" % (project_name, ACTION_LABEL.get(action, action)),
            project=project_name,
            action=action,
        )
        self._register(task)

        thread = threading.Thread(
            target=self._run, args=(task, project_dir, compose_path, compose_name, action), daemon=True
        )
        thread.start()
        return task, None

    def start_many(self, projects, action):
        """批量执行：生成一个总任务，串行处理多个项目。"""
        if action not in ACTION_STEPS:
            return None, "未知操作"
        if not self.docker.compose_prefix:
            return None, "docker compose 不可用，请确认 Docker 服务已安装并运行"

        valid = []
        for name in projects:
            project_dir, err = resolve_project(self.settings.get("base_path"), name)
            if err:
                continue
            compose_path, compose_name = find_compose_file(project_dir)
            if compose_path:
                valid.append((name, project_dir, compose_path, compose_name))

        if not valid:
            return None, "没有可执行的项目"

        task = Task("%s · 共 %d 个项目" % (ACTION_LABEL.get(action, action), len(valid)), action=action)
        self._register(task)
        thread = threading.Thread(target=self._run_many, args=(task, valid, action), daemon=True)
        thread.start()
        return task, None

    def _run_many(self, task, items, action):
        ok_count = 0
        total = len(items)
        base = self.settings.get("base_path") or ""
        try:
            for index, (name, project_dir, compose_path, compose_name) in enumerate(items, 1):
                task.add("")
                task.add("═══ [%d/%d] %s ═══" % (index, total, name), "head")

                if not self._claim(project_dir):
                    task.add("该项目正在执行其他操作，已跳过", "warn")
                    continue
                try:
                    if self._execute_steps(task, project_dir, compose_path, compose_name, action, base):
                        ok_count += 1
                finally:
                    self._release(project_dir)

            task.add("")
            task.add("批量操作完成：成功 %d / 共 %d" % (ok_count, total), "ok" if ok_count == total else "warn")
            task.finish(ok_count == total, 0 if ok_count == total else 1)
        except Exception as exc:  # noqa: BLE001
            task.add("执行异常: %s" % exc, "err")
            task.finish(False, 1)

    def _run(self, task, project_dir, compose_path, compose_name, action):
        base = self.settings.get("base_path") or ""
        try:
            success = self._execute_steps(task, project_dir, compose_path, compose_name, action, base)
            task.finish(success, 0 if success else 1)
        except Exception as exc:  # noqa: BLE001
            task.add("执行异常: %s" % exc, "err")
            task.finish(False, 1)
        finally:
            self._release(project_dir)

    def _execute_steps(self, task, project_dir, compose_path, compose_name, action, base):
        task.add("项目目录: %s" % project_dir, "dim")
        task.add("配置文件: %s" % compose_name, "dim")
        task.add("")

        steps = ACTION_STEPS[action]
        step_names = {
            "down": "停止并移除容器",
            "pull": "拉取最新镜像",
            "up": "启动容器",
            "restart": "重启容器",
        }

        for index, step in enumerate(steps, 1):
            args = ["-f", compose_name, step]
            if step == "up":
                args.append("-d")

            cmd = self.docker.command(args)
            task.add("▶ [%d/%d] %s  ($ docker compose %s)" % (index, len(steps), step_names[step], " ".join(args)), "head")

            code, step_lines = self._stream(task, cmd, project_dir)

            # 启动时若被外部创建的同名容器占用，自动清理后重试一次
            if code != 0 and step == "up":
                if self._recover_conflict(task, project_dir, compose_name, "\n".join(step_lines)):
                    task.add("↻ 重新启动容器", "head")
                    code, step_lines = self._stream(task, cmd, project_dir)

            if code != 0:
                task.add("✗ 步骤失败，退出码 %s" % code, "err")
                if step == "pull":
                    task.add("镜像拉取失败。若只是本地镜像无需更新，可忽略；否则请检查网络或镜像源。", "warn")
                if step == "up":
                    task.add("若容器名被其他程序占用，可先执行：docker ps -a | grep <容器名>", "warn")
                task.add("")
                task.add("操作中断于「%s」步骤" % step_names[step], "err")
                return False
            task.add("✓ %s 完成" % step_names[step], "ok")
            task.add("")

        if action in ("upgrade", "up", "restart"):
            self._append_status(task, project_dir, compose_name, base)

        task.add("全部步骤执行完成", "ok")
        return True

    def _compose_project_name(self, project_dir, compose_name):
        """取 compose 实际生效的项目名（可能是 yml 顶层的 name 字段）。"""
        cmd = self.docker.command(["-f", compose_name, "config", "--format", "json"])
        rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=20)
        if rc != 0 or not out.strip():
            return ""
        try:
            return str((json.loads(out) or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _recover_conflict(self, task, project_dir, compose_name, text):
        """处理「容器名已被占用」：仅清理不属于其他 Compose 项目的同名容器。"""
        match = CONFLICT_RE.search(text or "")
        if not match:
            return False

        name = match.group("name")
        task.add("检测到同名容器占用：%s" % name, "warn")

        rc, owner, err = run_cmd(
            ["docker", "inspect", "-f", '{{index .Config.Labels "com.docker.compose.project"}}', name],
            timeout=15,
        )
        owner = (owner or "").strip()

        allowed = {os.path.basename(project_dir.rstrip("/"))}
        project_name = self._compose_project_name(project_dir, compose_name)
        if project_name:
            allowed.add(project_name)

        if owner and owner not in allowed:
            task.add("该容器属于另一个 Compose 项目「%s」，为避免影响其它服务，未自动删除。" % owner, "warn")
            task.add("请确认后手动执行：docker rm -f %s，然后重试。" % name, "warn")
            return False

        rc2, out2, err2 = run_cmd(["docker", "rm", "-f", name], timeout=90)
        if rc2 != 0:
            detail = (err2 or out2 or "未知错误").splitlines()[0][:160]
            task.add("删除同名容器失败：%s" % detail, "err")
            return False

        task.add("已删除外部创建的同名容器 %s" % name, "ok")
        return True

    def _append_status(self, task, project_dir, compose_name, base):
        cmd = self.docker.command(["-f", compose_name, "ps", "--format", "json"])
        rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=20)
        containers = parse_json_ps(out) if rc == 0 else None

        if containers is None:
            cmd = self.docker.command(
                ["-f", compose_name, "ps", "--format", "{{.Name}}|{{.Service}}|{{.State}}|{{.Status}}|{{.Image}}"]
            )
            rc, out, err = run_cmd(cmd, cwd=project_dir, timeout=20)
            containers = parse_ps_output(out) if rc == 0 else []

        if not containers:
            return

        task.add("当前容器状态：", "head")
        for c in containers:
            mark = "●" if c.get("state") == "running" else "○"
            text = "%s %s  %s  %s" % (mark, c.get("name") or c.get("service"), c.get("state"), c.get("status"))
            task.add("  " + text.strip(), "ok" if c.get("state") == "running" else "dim")

    def _stream(self, task, cmd, cwd):
        """流式执行命令，返回 (退出码, 输出行列表)。"""
        env = os.environ.copy()
        env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        env["COMPOSE_INTERACTIVE_NO_CLI"] = "1"
        env["DOCKER_CLI_HINTS"] = "false"
        collected = []
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
                errors="replace",
            )
        except FileNotFoundError:
            task.add("找不到 docker 命令", "err")
            return 127, collected
        except Exception as exc:  # noqa: BLE001
            task.add("启动命令失败: %s" % exc, "err")
            return 1, collected

        try:
            for line in iter(proc.stdout.readline, ""):
                line = line.rstrip("\n")
                if line.strip():
                    collected.append(line)
                    task.add(line, self._classify(line))
            proc.wait()
        except Exception as exc:  # noqa: BLE001
            task.add("读取输出失败: %s" % exc, "err")
            try:
                proc.kill()
            except Exception:
                pass
            return 1, collected
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        return proc.returncode or 0, collected

    @staticmethod
    def _classify(line):
        low = line.lower()
        if any(k in low for k in ("error", "failed", "失败", "denied", "no such", "cannot")):
            return "err"
        if any(k in low for k in ("warning", "warn", "deprecated")):
            return "warn"
        if line.startswith(" ") or line.startswith("✔") or "started" in low or "running" in low:
            return "ok"
        return "out"


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}


class AppContext(object):
    def __init__(self, settings, docker_env, scanner, tasks, static_dir):
        self.settings = settings
        self.docker = docker_env
        self.scanner = scanner
        self.tasks = tasks
        self.static_dir = static_dir


class Handler(BaseHTTPRequestHandler):
    server_version = "DockerUp/" + APP_VERSION
    protocol_version = "HTTP/1.1"
    ctx = None  # type: AppContext

    # ---------------- 基础响应 ----------------

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (now_str(), fmt % args))
        sys.stdout.flush()

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body)

    def _error(self, message, code=400):
        self._json({"ok": False, "error": message}, code)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1 << 20:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------------- 路由 ----------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path.startswith("/api/"):
                return self._api_get(path, query)
            return self._static(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            self._error("服务内部错误: %s" % exc, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            return self._api_post(path)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            self._error("服务内部错误: %s" % exc, 500)

    def _api_get(self, path, query):
        ctx = self.ctx

        if path == "/api/bootstrap":
            return self._json(self._bootstrap())

        if path == "/api/task":
            task_id = (query.get("id") or [""])[0]
            offset = (query.get("offset") or ["0"])[0]
            try:
                offset = int(offset)
            except ValueError:
                offset = 0
            task = ctx.tasks.get(task_id) if task_id else ctx.tasks.latest()
            if not task:
                return self._json({"ok": True, "task": None})
            return self._json({"ok": True, "task": task.snapshot(offset)})

        if path == "/api/logs":
            name = (query.get("project") or [""])[0]
            try:
                tail = int((query.get("tail") or ["200"])[0])
            except ValueError:
                tail = 200
            tail = max(10, min(2000, tail))
            container = (query.get("container") or [""])[0]
            return self._json(self._logs(name, tail, container))

        if path == "/api/compose":
            name = (query.get("project") or [""])[0]
            return self._json(self._compose_content(name))

        if path == "/api/health":
            return self._json({"ok": True, "version": APP_VERSION})

        return self._error("接口不存在", 404)

    def _api_post(self, path):
        ctx = self.ctx
        body = self._read_json()

        if path == "/api/scan":
            return self._json({"ok": True, "data": ctx.scanner.scan()})

        if path == "/api/settings":
            base_path = str(body.get("base_path") or "").strip()
            if not base_path:
                return self._error("目录路径不能为空")
            if not base_path.startswith("/"):
                return self._error("请填写绝对路径，例如 /vol1/1000/docker")
            base_path = base_path.rstrip("/") or "/"
            if not os.path.isdir(base_path):
                return self._error("目录不存在：%s" % base_path)
            if not os.access(base_path, os.R_OK):
                return self._error("目录无读取权限：%s" % base_path)
            ctx.settings.set("base_path", base_path)
            return self._json({"ok": True, "data": ctx.scanner.scan()})

        if path == "/api/action":
            action = str(body.get("action") or "")
            project = str(body.get("project") or "")
            projects = body.get("projects")

            if isinstance(projects, list) and projects:
                names = [str(p) for p in projects][:200]
                task, err = ctx.tasks.start_many(names, action)
            else:
                if not project:
                    return self._error("缺少项目名")
                task, err = ctx.tasks.start(project, action)

            if err:
                return self._error(err)
            return self._json({"ok": True, "task_id": task.id})

        if path == "/api/detect-base":
            return self._json({"ok": True, "paths": self._detect_base_paths()})

        return self._error("接口不存在", 404)

    # ---------------- 具体逻辑 ----------------

    def _bootstrap(self):
        ctx = self.ctx
        data = ctx.scanner.scan()
        return {
            "ok": True,
            "version": APP_VERSION,
            "docker": ctx.docker.info(),
            "scan": data,
            "server_time": now_str(),
        }

    def _logs(self, project_name, tail, container=""):
        ctx = self.ctx
        project_dir, err = resolve_project(ctx.settings.get("base_path"), project_name)
        if err:
            return {"ok": False, "error": err}
        compose_path, compose_name = find_compose_file(project_dir)
        if not compose_path:
            return {"ok": False, "error": "未找到 compose 文件"}

        # 指定了容器：直接用 docker logs 读取（对 compose 管理和外部创建的容器都有效）
        if container:
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", container):
                return {"ok": False, "error": "容器名无效"}
            docker_bin = shutil.which("docker") or "/usr/bin/docker"
            rc, out, err_text = run_cmd(
                [docker_bin, "logs", "--tail", str(tail), container], timeout=40
            )
            if rc != 0:
                return {"ok": False, "error": (err_text or out or "读取日志失败")[:300]}
            combined = "\n".join(x for x in (out, err_text) if x)
            return {"ok": True, "logs": combined[-200000:] or "（暂无日志输出）"}

        cmd = ctx.docker.command(["-f", compose_name, "logs", "--tail", str(tail), "--no-color"])
        if not cmd:
            return {"ok": False, "error": "docker compose 不可用"}
        rc, out, err_text = run_cmd(cmd, cwd=project_dir, timeout=40)
        if rc != 0:
            return {"ok": False, "error": (err_text or "读取日志失败")[:300]}
        return {"ok": True, "logs": out[-200000:]}

    def _compose_content(self, project_name):
        """读取项目的 compose 配置文件内容，仅展示不执行。"""
        ctx = self.ctx
        project_dir, err = resolve_project(ctx.settings.get("base_path"), project_name)
        if err:
            return {"ok": False, "error": err}
        compose_path, compose_name = find_compose_file(project_dir)
        if not compose_path:
            return {"ok": False, "error": "未找到 compose 文件"}
        try:
            size = os.path.getsize(compose_path)
            if size > 512 * 1024:
                return {"ok": False, "error": "compose 文件过大（%d KB），不予展示" % (size // 1024)}
            with open(compose_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "读取失败: %s" % exc}
        return {"ok": True, "file": compose_name, "path": compose_path, "content": content}

    def _detect_base_paths(self):
        """在常见位置寻找 docker 项目目录，方便用户第一次配置。"""
        found = []
        candidates = []
        for vol in range(1, 9):
            root = "/vol%d" % vol
            if not os.path.isdir(root):
                continue
            try:
                for user_dir in sorted(os.listdir(root)):
                    if user_dir.startswith(".") or user_dir.startswith("@"):
                        continue
                    candidates.append(os.path.join(root, user_dir, "docker"))
            except Exception:
                continue
            candidates.append(os.path.join(root, "docker"))

        for path in candidates:
            if not os.path.isdir(path):
                continue
            count = 0
            try:
                for name in os.listdir(path)[:200]:
                    sub = os.path.join(path, name)
                    if os.path.isdir(sub) and find_compose_file(sub)[0]:
                        count += 1
            except Exception:
                continue
            if count > 0:
                found.append({"path": path, "projects": count})

        found.sort(key=lambda item: item["projects"], reverse=True)
        return found[:8]

    def _static(self, path):
        static_dir = os.path.realpath(self.ctx.static_dir)
        if path in ("/", ""):
            path = "/index.html"

        target = os.path.realpath(os.path.join(static_dir, path.lstrip("/")))
        if not target.startswith(static_dir + os.sep) and target != static_dir:
            return self._send(403, b"Forbidden", "text/plain; charset=utf-8")
        if not os.path.isfile(target):
            # 单页应用回退
            fallback = os.path.join(static_dir, "index.html")
            if os.path.isfile(fallback) and "." not in os.path.basename(path):
                target = fallback
            else:
                return self._send(404, b"Not Found", "text/plain; charset=utf-8")

        ext = os.path.splitext(target)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except Exception:
            return self._send(500, b"Read Error", "text/plain; charset=utf-8")
        self._send(200, body, ctype)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args(argv):
    opts = {"port": 39190, "static": "", "data": ""}
    index = 1
    while index < len(argv):
        key = argv[index]
        if key.startswith("--") and index + 1 < len(argv):
            opts[key[2:]] = argv[index + 1]
            index += 2
        else:
            index += 1

    if not opts["static"]:
        here = os.path.dirname(os.path.abspath(__file__))
        opts["static"] = os.path.normpath(os.path.join(here, "..", "ui", "www"))
    if not opts["data"]:
        opts["data"] = os.path.dirname(os.path.abspath(__file__))
    try:
        opts["port"] = int(opts["port"])
    except (TypeError, ValueError):
        opts["port"] = 39190
    return opts


def main():
    opts = parse_args(sys.argv)
    os.makedirs(opts["data"], exist_ok=True)

    settings = Settings(opts["data"])
    docker_env = DockerEnv()
    docker_env.detect()
    scanner = ProjectScanner(settings, docker_env)
    tasks = TaskManager(settings, docker_env)

    Handler.ctx = AppContext(settings, docker_env, scanner, tasks, opts["static"])

    port = opts["port"]
    server = None
    for attempt in range(12):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port + attempt), Handler)
            if attempt:
                print("[info] 端口 %d 被占用，改用 %d" % (port, port + attempt))
            port = port + attempt
            break
        except OSError as exc:
            print("[warn] 绑定端口 %d 失败: %s" % (port + attempt, exc))
            continue

    if server is None:
        print("[error] 无法绑定任何端口，退出")
        sys.exit(1)

    server.daemon_threads = True
    print("[info] 服务管理 v%s 已启动: http://0.0.0.0:%d" % (APP_VERSION, port))
    print("[info] 静态目录: %s" % opts["static"])
    print("[info] 数据目录: %s" % opts["data"])
    print("[info] 项目根目录: %s" % settings.get("base_path"))
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[info] 收到中断信号，退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
