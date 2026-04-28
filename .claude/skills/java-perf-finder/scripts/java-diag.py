#!/usr/bin/env python3
"""
java-diag.py — Java Performance Diagnostic Script
Cross-platform (Linux / Windows / macOS), Python 3.6+, zero required dependencies.
Optional: psutil (for richer process listing), Arthas (for enhanced diagnosis).

Usage:
  python java-diag.py                     # List Java processes, pick one
  python java-diag.py -p <pid>            # Diagnose specific process
  python java-diag.py -p <pid> --full     # Full mode (Arthas + heap dump + services)
  python java-diag.py -p <pid> --lite     # Lite mode (JDK tools only)
  python java-diag.py --check-redis host:port --check-mysql host:port
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from html import escape
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.0.0"
ARTHAS_URL = "https://arthas.aliyun.com/arthas-boot.jar"
DEFAULT_TIMEOUT = 30
GC_SNAPSHOT_COUNT = 3
GC_SNAPSHOT_INTERVAL_MS = 1000
REPORT_DIR_PREFIX = "diag-report-"

# Arthas batch commands
ARTHAS_DIAG_SCRIPT = """\
dashboard -n 1
thread -n 10
thread -b
memory
jvm
classloader
"""

ARTHAS_SQL_TRACE = """\
trace java.sql.Statement execute* -n 5
trace com.zaxxer.hikari.HikariDataSource getConnection -n 5
trace com.alibaba.druid.pool.DruidDataSource getConnection -n 5
"""

ARTHAS_REDIS_TRACE = """\
trace org.springframework.data.redis.core.RedisTemplate execute -n 5
trace redis.clients.jedis.Jedis get -n 5
"""

# Default thread name patterns to IGNORE (system / JVM internals)
IGNORED_THREAD_PATTERNS = [
    r"^Attach Listener$",
    r"^C\d+ CompilerThread",
    r"^C\d+ sweeper",
    r"^Service Thread$",
    r"^GC task",
    r"^VM Thread",
    r"^Reference Handler$",
    r"^Finalizer$",
    r"^Signal Dispatcher$",
    r"^JDWP Transport Listener",
    r"^JDWP Event Helper",
    r"^arthas-",
    r"^JFR request timer",
    r"^DestroyJavaVM$",
    r"^Java2D Disposer",
    r"^AWT-",
    r"^NioBlockingSelector",
    r"^Abandoned connection",
    r"^process reaper",
    r"^elasticsearch",
    r"^Timer-\d+$",
    r"^pool-\d+-thread-\d+$",  # generic pool threads (unless matched by filter)
]

# Severity thresholds
HEAP_WARN_PCT = 70
HEAP_CRIT_PCT = 90
REDIS_LATENCY_WARN_MS = 50
REDIS_LATENCY_CRIT_MS = 200
DB_LATENCY_WARN_MS = 100
DB_LATENCY_CRIT_MS = 500

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "WARN": "! ", "ERROR": "X ", "OK": "+ "}.get(level, "  ")
    print(f"[{ts}] {prefix}{msg}", flush=True)


def run_cmd(cmd, timeout=DEFAULT_TIMEOUT, check=False):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace"
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -2, "", f"Command not found: {cmd[0] if isinstance(cmd, list) else cmd}"
    except Exception as e:
        return -3, "", str(e)


def find_jdk_tool(name):
    """Locate a JDK tool: JAVA_HOME/bin first, then PATH."""
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        for ext in ("", ".exe"):
            path = os.path.join(java_home, "bin", name + ext)
            if os.path.isfile(path):
                return path
    found = shutil.which(name)
    return found


def ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Phase 1: Environment Discovery
# ---------------------------------------------------------------------------

def detect_environment():
    """Detect OS, JAVA_HOME, available JDK tools."""
    env = {
        "os": platform.system(),
        "os_version": platform.version(),
        "python": platform.python_version(),
        "java_home": os.environ.get("JAVA_HOME", ""),
        "jdk_tools": {},
    }
    log(f"OS: {env['os']} {env['os_version']}")
    log(f"Python: {env['python']}")
    log(f"JAVA_HOME: {env['java_home'] or '(not set)'}")

    for tool in ("jcmd", "jps", "jstack", "jmap", "jstat", "jinfo", "java"):
        path = find_jdk_tool(tool)
        env["jdk_tools"][tool] = path
        if path:
            log(f"Found {tool}: {path}")
        else:
            log(f"NOT found: {tool}", "WARN")

    return env


def list_java_processes(env):
    """List Java processes via jcmd -l or psutil."""
    processes = []

    # Method 1: jcmd -l (preferred)
    jcmd = env["jdk_tools"].get("jcmd")
    if jcmd:
        rc, out, _ = run_cmd([jcmd, "-l"])
        if rc == 0:
            for line in out.strip().splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) >= 2 and parts[0].isdigit():
                    pid = int(parts[0])
                    main_class = parts[1]
                    is_spring = _is_spring_boot(main_class)
                    processes.append({
                        "pid": pid,
                        "main_class": main_class,
                        "is_spring": is_spring,
                        "source": "jcmd",
                    })

    # Method 2: jps -lv (fallback)
    if not processes:
        jps = env["jdk_tools"].get("jps")
        if jps:
            rc, out, _ = run_cmd([jps, "-lv"])
            if rc == 0:
                for line in out.strip().splitlines():
                    parts = line.strip().split(None, 2)
                    if len(parts) >= 2 and parts[0].isdigit():
                        pid = int(parts[0])
                        main_class = parts[1] if len(parts) > 1 else ""
                        jvm_args = parts[2] if len(parts) > 2 else ""
                        is_spring = _is_spring_boot(main_class + " " + jvm_args)
                        processes.append({
                            "pid": pid,
                            "main_class": main_class,
                            "is_spring": is_spring,
                            "source": "jps",
                        })

    # Method 3: psutil (last resort)
    if not processes:
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    name = (proc.info["name"] or "").lower()
                    cmdline = " ".join(proc.info["cmdline"] or [])
                    if "java" in name or "java" in cmdline.lower():
                        is_spring = _is_spring_boot(cmdline)
                        processes.append({
                            "pid": proc.info["pid"],
                            "main_class": _extract_main_class(cmdline),
                            "is_spring": is_spring,
                            "source": "psutil",
                        })
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
        except ImportError:
            pass

    return processes


def _is_spring_boot(text):
    markers = [
        "org.springframework.boot",
        "spring-boot",
        "SpringApplication",
        "JarLauncher",
        "WarLauncher",
    ]
    text_lower = text.lower()
    return any(m.lower() in text_lower for m in markers)


def _extract_main_class(cmdline):
    tokens = cmdline.split()
    for i, t in enumerate(tokens):
        if t == "-jar" and i + 1 < len(tokens):
            return tokens[i + 1]
    for t in tokens:
        if "." in t and not t.startswith("-") and t[0].isupper():
            return t
    return cmdline[:80]


def locate_arthas(target_dir, local_jar=None):
    """Locate Arthas boot jar: (1) user-specified path, (2) existing in output dir, (3) common paths, (4) download."""
    # 1. User-specified local jar
    if local_jar:
        if os.path.isfile(local_jar):
            log(f"Using user-specified Arthas: {local_jar}", "OK")
            return local_jar
        else:
            log(f"Specified Arthas jar not found: {local_jar}", "ERROR")
            return None

    # 2. Already downloaded in output dir
    arthas_path = os.path.join(target_dir, "arthas-boot.jar")
    if os.path.isfile(arthas_path):
        log(f"Arthas already exists: {arthas_path}")
        return arthas_path

    # 3. Search common locations
    common_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "arthas-boot.jar"),
        os.path.join(os.path.expanduser("~"), "arthas-boot.jar"),
        os.path.join(os.path.expanduser("~"), ".arthas", "arthas-boot.jar"),
        os.path.join(tempfile.gettempdir(), "arthas-boot.jar"),
    ]
    for p in common_paths:
        if os.path.isfile(p):
            log(f"Found Arthas at: {p}", "OK")
            return p

    # 4. Try download (will fail in intranet)
    log(f"Arthas not found locally, trying download from {ARTHAS_URL} ...")
    log("If this is an intranet environment, use --arthas-jar to specify the local path", "WARN")
    try:
        urllib.request.urlretrieve(ARTHAS_URL, arthas_path)
        log("Arthas downloaded successfully", "OK")
        return arthas_path
    except Exception as e:
        log(f"Failed to download Arthas: {e}", "ERROR")
        log("This is likely an intranet environment. Please:", "ERROR")
        log("  1. Download arthas-boot.jar on an internet-connected machine:", "ERROR")
        log(f"     wget {ARTHAS_URL}", "ERROR")
        log("  2. Copy it to the server and use:", "ERROR")
        log("     python java-diag.py -p <pid> --full --arthas-jar /path/to/arthas-boot.jar", "ERROR")
        return None


# ---------------------------------------------------------------------------
# Phase 2: JDK Tool Data Collection
# ---------------------------------------------------------------------------

def collect_jdk_data(pid, env, output_dir, lite=False):
    """Collect diagnostic data using JDK built-in tools."""
    jcmd = env["jdk_tools"].get("jcmd")
    jstack = env["jdk_tools"].get("jstack")
    jmap = env["jdk_tools"].get("jmap")
    jstat = env["jdk_tools"].get("jstat")
    jinfo = env["jdk_tools"].get("jinfo")

    data = {}
    raw_dir = os.path.join(output_dir, "raw")
    ensure_dir(raw_dir)

    # -- jcmd commands --
    jcmd_tasks = {
        "threaddump": ["Thread.print", "-l"],
        "heap_info": ["GC.heap_info"],
        "class_histogram": ["GC.class_histogram"],
        "vm_flags": ["VM.flags", "-all"],
        "vm_command_line": ["VM.command_line"],
        "vm_sysprops": ["VM.system_properties"],
        "vm_classloader": ["VM.classloader_stats"],
        "vm_info": ["VM.info"],
    }

    for name, args in jcmd_tasks.items():
        if not jcmd:
            continue
        log(f"Collecting {name} via jcmd ...")
        cmd = [jcmd, str(pid)] + args
        rc, out, err = run_cmd(cmd, timeout=60)
        data[name] = out
        filepath = os.path.join(raw_dir, f"{name}.txt")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(out)
            if err:
                f.write("\n\n--- STDERR ---\n")
                f.write(err)
        if rc != 0:
            log(f"  jcmd {args[0]} failed (rc={rc}): {err.strip()[:100]}", "WARN")

    # -- jstat: GC snapshots --
    if jstat:
        log(f"Collecting GC stats ({GC_SNAPSHOT_COUNT} snapshots, {GC_SNAPSHOT_INTERVAL_MS}ms interval) ...")
        cmd = [jstat, "-gcutil", str(pid), str(GC_SNAPSHOT_INTERVAL_MS), str(GC_SNAPSHOT_COUNT)]
        rc, out, err = run_cmd(cmd, timeout=GC_SNAPSHOT_COUNT * 3 + 10)
        data["gc_stats"] = out
        with open(os.path.join(raw_dir, "gc_stats.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        if rc != 0:
            log(f"  jstat failed (rc={rc}): {err.strip()[:100]}", "WARN")

    # -- Heap dump (only in --full mode) --
    if not lite and jcmd:
        heap_dump_path = os.path.join(raw_dir, "heap.hprof")
        log("Collecting heap dump (this may take a while) ...")
        cmd = [jcmd, str(pid), "GC.heap_dump", heap_dump_path]
        rc, out, err = run_cmd(cmd, timeout=300)
        if rc == 0:
            data["heap_dump"] = heap_dump_path
            size_mb = os.path.getsize(heap_dump_path) / (1024 * 1024) if os.path.isfile(heap_dump_path) else 0
            log(f"  Heap dump saved: {heap_dump_path} ({size_mb:.1f} MB)", "OK")
        else:
            log(f"  Heap dump failed: {err.strip()[:100]}", "WARN")

    # ── Fallbacks: when jcmd is unavailable or failed ──

    # Fallback: jstack for threaddump
    if not data.get("threaddump") and jstack:
        log("Falling back to jstack for thread dump ...")
        cmd = [jstack, "-l", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=60)
        data["threaddump"] = out
        with open(os.path.join(raw_dir, "threaddump.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    # Fallback: jmap -heap for heap_info
    if not data.get("heap_info") and jmap:
        log("Falling back to jmap -heap for heap info ...")
        cmd = [jmap, "-heap", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=60)
        data["heap_info"] = out
        with open(os.path.join(raw_dir, "heap_info.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    # Fallback: jmap -histo for class_histogram
    if not data.get("class_histogram") and jmap:
        log("Falling back to jmap -histo for class histogram ...")
        cmd = [jmap, "-histo", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=120)
        data["class_histogram"] = out
        with open(os.path.join(raw_dir, "class_histogram.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    # Fallback: jinfo -flags for vm_flags
    if not data.get("vm_flags") and jinfo:
        log("Falling back to jinfo -flags for VM flags ...")
        cmd = [jinfo, "-flags", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=30)
        data["vm_flags"] = out
        with open(os.path.join(raw_dir, "vm_flags.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    # Fallback: jinfo -sysprops for vm_sysprops
    if not data.get("vm_sysprops") and jinfo:
        log("Falling back to jinfo -sysprops for system properties ...")
        cmd = [jinfo, "-sysprops", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=30)
        data["vm_sysprops"] = out
        with open(os.path.join(raw_dir, "vm_sysprops.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    # Fallback: jps -v for vm_command_line (shows JVM args and main class)
    if not data.get("vm_command_line"):
        jps = env["jdk_tools"].get("jps")
        if jps:
            log("Falling back to jps -v for VM command line ...")
            cmd = [jps, "-v"]
            rc, out, err = run_cmd(cmd, timeout=15)
            if rc == 0:
                # Extract the line for our PID
                for line in out.strip().splitlines():
                    parts = line.strip().split(None, 1)
                    if parts and parts[0] == str(pid):
                        data["vm_command_line"] = line
                        break
                else:
                    data["vm_command_line"] = out
            with open(os.path.join(raw_dir, "vm_command_line.txt"), "w", encoding="utf-8") as f:
                f.write(data.get("vm_command_line", ""))

    # Fallback: jmap -dump for heap_dump (only in --full mode)
    if not lite and not data.get("heap_dump") and jmap:
        heap_dump_path = os.path.join(raw_dir, "heap.hprof")
        log("Falling back to jmap -dump for heap dump ...")
        cmd = [jmap, f"-dump:format=b,file={heap_dump_path}", str(pid)]
        rc, out, err = run_cmd(cmd, timeout=300)
        if rc == 0 and os.path.isfile(heap_dump_path):
            data["heap_dump"] = heap_dump_path
            size_mb = os.path.getsize(heap_dump_path) / (1024 * 1024)
            log(f"  Heap dump saved: {heap_dump_path} ({size_mb:.1f} MB)", "OK")
        else:
            log(f"  jmap heap dump failed: {err.strip()[:100]}", "WARN")

    return data


# ---------------------------------------------------------------------------
# Phase 3: Arthas Enhanced Collection
# ---------------------------------------------------------------------------

def collect_arthas_data(pid, env, output_dir, local_jar=None):
    """Collect enhanced data via Arthas batch mode."""
    java = env["jdk_tools"].get("java")
    if not java:
        log("Java not found, skipping Arthas", "WARN")
        return {}

    arthas_jar = locate_arthas(output_dir, local_jar=local_jar)
    if not arthas_jar:
        return {}

    data = {}
    raw_dir = os.path.join(output_dir, "raw")

    # Generate Arthas script
    script_path = os.path.join(output_dir, "arthas-diag.as")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(ARTHAS_DIAG_SCRIPT)

    log("Attaching Arthas (this may take 10-20s) ...")
    cmd = [java, "-jar", arthas_jar, "--select", str(pid), "-f", script_path]
    rc, out, err = run_cmd(cmd, timeout=120)

    if rc == 0:
        data["arthas_diag"] = out
        with open(os.path.join(raw_dir, "arthas_diag.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        log("Arthas data collected", "OK")
    else:
        log(f"Arthas attachment failed: {err.strip()[:200]}", "ERROR")
        data["arthas_diag"] = out + "\n\n--- STDERR ---\n" + err

    # Optional: SQL trace
    sql_script = os.path.join(output_dir, "arthas-sql.as")
    with open(sql_script, "w", encoding="utf-8") as f:
        f.write(ARTHAS_SQL_TRACE)
    log("Running Arthas SQL trace (5 captures) ...")
    rc2, out2, err2 = run_cmd(
        [java, "-jar", arthas_jar, "--select", str(pid), "-f", sql_script],
        timeout=60
    )
    if rc2 == 0:
        data["arthas_sql"] = out2
        with open(os.path.join(raw_dir, "arthas_sql.txt"), "w", encoding="utf-8") as f:
            f.write(out2)

    # Optional: Redis trace
    redis_script = os.path.join(output_dir, "arthas-redis.as")
    with open(redis_script, "w", encoding="utf-8") as f:
        f.write(ARTHAS_REDIS_TRACE)
    log("Running Arthas Redis trace (5 captures) ...")
    rc3, out3, err3 = run_cmd(
        [java, "-jar", arthas_jar, "--select", str(pid), "-f", redis_script],
        timeout=60
    )
    if rc3 == 0:
        data["arthas_redis"] = out3
        with open(os.path.join(raw_dir, "arthas_redis.txt"), "w", encoding="utf-8") as f:
            f.write(out3)

    return data


def trace_hot_methods(pid, env, hot_methods, output_dir, local_jar=None, trace_count=5):
    """
    Given hot method names from sampling analysis (e.g. 'OrderService.createOrder'),
    run Arthas trace on each to break down internal call costs.

    This turns "thread stuck in OrderService.createOrder 80% of the time"
    into "createOrder spends 650ms in itemMapper.selectByOrderId, 50ms in rest".
    """
    java = env["jdk_tools"].get("java")
    if not java:
        log("Java not found, cannot trace hot methods", "WARN")
        return {}

    arthas_jar = locate_arthas(output_dir, local_jar=local_jar)
    if not arthas_jar:
        log("Arthas not available, skipping hot method tracing", "WARN")
        log("To enable: --arthas-jar /path/to/arthas-boot.jar", "WARN")
        return {}

    raw_dir = os.path.join(output_dir, "raw")
    ensure_dir(raw_dir)
    trace_results = {}

    for method in hot_methods:
        # Convert short name back to trace pattern
        # "OrderService.createOrder" -> trace with class wildcard pattern
        # We don't have full package, so use class simple name as wildcard
        trace_pattern = f"*{method}"
        log(f"Tracing hot method: {method} (pattern: {trace_pattern}) ...")

        script_content = f"trace {trace_pattern} -n {trace_count}\n"
        script_path = os.path.join(output_dir, f"trace_{method.replace('.', '_')}.as")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        rc, out, err = run_cmd(
            [java, "-jar", arthas_jar, "--select", str(pid), "-f", script_path],
            timeout=60
        )

        if rc == 0 and out:
            trace_results[method] = out
            filepath = os.path.join(raw_dir, f"trace_{method.replace('.', '_')}.txt")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(out)
            log(f"  Trace captured for {method}", "OK")
        else:
            log(f"  Trace failed for {method}: {err.strip()[:100]}", "WARN")
            trace_results[method] = f"TRACE_FAILED\n{err}"

    return trace_results


# ---------------------------------------------------------------------------
# Phase 4: External Service Checks
# ---------------------------------------------------------------------------

def check_redis(host, port, password=None, timeout=3):
    """Redis PING check via raw socket (zero dependencies)."""
    result = {"host": host, "port": port, "status": "UNKNOWN", "connect_ms": None, "ping_ms": None}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.connect((host, port))
        result["connect_ms"] = round((time.time() - t0) * 1000, 2)

        if password:
            auth_cmd = f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n"
            s.sendall(auth_cmd.encode())
            s.recv(1024)

        ping_cmd = "*1\r\n$4\r\nPING\r\n"
        t1 = time.time()
        s.sendall(ping_cmd.encode())
        resp = s.recv(1024)
        result["ping_ms"] = round((time.time() - t1) * 1000, 2)
        result["status"] = "OK" if b"PONG" in resp else "UNEXPECTED"
        s.close()
    except socket.timeout:
        result["status"] = "TIMEOUT"
    except ConnectionRefusedError:
        result["status"] = "CONNECTION_REFUSED"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
    return result


def check_mysql(host, port, timeout=3):
    """MySQL connectivity check via socket handshake."""
    result = {"host": host, "port": port, "status": "UNKNOWN", "connect_ms": None, "handshake_ms": None}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.connect((host, port))
        result["connect_ms"] = round((time.time() - t0) * 1000, 2)

        # MySQL server sends greeting packet on connect
        t1 = time.time()
        greeting = s.recv(1024)
        result["handshake_ms"] = round((time.time() - t1) * 1000, 2)
        result["status"] = "OK" if len(greeting) > 0 else "NO_GREETING"

        # Extract version from greeting (first null-terminated string after protocol version)
        try:
            if greeting and greeting[0] == 10:  # protocol version 10
                version_end = greeting.index(0, 1)
                result["server_version"] = greeting[1:version_end].decode("utf-8", errors="replace")
        except (ValueError, IndexError):
            pass

        s.close()
    except socket.timeout:
        result["status"] = "TIMEOUT"
    except ConnectionRefusedError:
        result["status"] = "CONNECTION_REFUSED"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
    return result


def check_postgresql(host, port, timeout=3):
    """PostgreSQL connectivity check via startup cancel message."""
    result = {"host": host, "port": port, "status": "UNKNOWN", "connect_ms": None}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.time()
        s.connect((host, port))
        result["connect_ms"] = round((time.time() - t0) * 1000, 2)

        # Send SSL request then cancel to get a response
        # PostgreSQL responds to invalid startup with an error that confirms it's PG
        cancel_msg = b"\x00\x00\x00\x10\x04\xd2\x16\x2f\x00\x00\x00\x00\x00\x00\x00\x00"
        s.sendall(cancel_msg)
        resp = s.recv(1024)
        # PG typically responds with error 'N' (Notice) or 'E' (Error) type byte
        result["status"] = "OK" if resp else "NO_RESPONSE"
        if resp:
            try:
                result["response_hint"] = resp.decode("utf-8", errors="replace")[:100]
            except Exception:
                pass
        s.close()
    except socket.timeout:
        result["status"] = "TIMEOUT"
    except ConnectionRefusedError:
        result["status"] = "CONNECTION_REFUSED"
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
    return result


def check_actuator(base_url, timeout=5):
    """Probe Spring Boot Actuator endpoints."""
    result = {"base_url": base_url, "endpoints": {}}
    endpoints = ["health", "info", "metrics", "threaddump", "heapdump", "env", "beans", "loggers"]

    for ep in endpoints:
        url = f"{base_url}/{ep}"
        try:
            req = urllib.request.Request(url)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=timeout)
            elapsed = round((time.time() - t0) * 1000, 2)
            body = resp.read().decode("utf-8", errors="replace")[:2000]
            result["endpoints"][ep] = {
                "status": "OK",
                "http_code": resp.status,
                "latency_ms": elapsed,
                "body_preview": body[:500],
            }
        except urllib.error.HTTPError as e:
            result["endpoints"][ep] = {"status": "HTTP_ERROR", "http_code": e.code}
        except Exception as e:
            result["endpoints"][ep] = {"status": "ERROR", "error": str(e)[:200]}

    return result


# ---------------------------------------------------------------------------
# Phase 2.5: Thread Stack Sampling (jstack sampler)
# ---------------------------------------------------------------------------

def run_stack_sampling(pid, env, count=10, interval=1, thread_filter=None, output_dir=None):
    """
    Collect N jstack snapshots at given interval, then analyze hot methods.

    This is the most precise way to find slow code:
    1. Ask user to trigger the slow operation (call API, click button, etc.)
    2. While the app is slow, take multiple jstack snapshots
    3. For each business thread, count how often each stack frame appears
    4. The method that appears most often = the bottleneck

    Returns: sampling analysis results dict
    """
    jcmd = env["jdk_tools"].get("jcmd")
    jstack = env["jdk_tools"].get("jstack")

    if not jcmd and not jstack:
        log("No jcmd or jstack available, cannot sample", "ERROR")
        return None

    log(f"Stack sampling: {count} snapshots, {interval}s interval")
    log(f"PID: {pid}")
    if thread_filter:
        log(f"Thread filter: {thread_filter}")

    print()
    print("  ┌────────────────────────────────────────────────────────────┐")
    print("  │  STACK SAMPLING MODE                                      │")
    print("  │                                                            │")
    print("  │  NOW: Trigger the slow operation in your application!      │")
    print("  │       Call the API, click the button, reproduce the lag.   │")
    print("  │                                                            │")
    print(f"  │  Sampling {count} snapshots, {interval}s apart...                 │")
    print("  │  Press Ctrl+C to stop early.                               │")
    print("  └────────────────────────────────────────────────────────────┘")
    print()

    dumps = []
    for i in range(count):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            if jcmd:
                rc, out, err = run_cmd([jcmd, str(pid), "Thread.print", "-l"], timeout=15)
            else:
                rc, out, err = run_cmd([jstack, "-l", str(pid)], timeout=15)
            if rc == 0 and out:
                dumps.append(out)
                log(f"  Sample {i+1}/{count} captured at {ts}", "OK")
            else:
                log(f"  Sample {i+1}/{count} FAILED: {err.strip()[:80]}", "WARN")
        except KeyboardInterrupt:
            log("\n  Sampling stopped by user (Ctrl+C)", "WARN")
            break

        if i < count - 1:
            time.sleep(interval)

    if not dumps:
        log("No samples collected", "ERROR")
        return None

    log(f"\nCollected {len(dumps)} samples, analyzing ...")

    # Save raw samples
    if output_dir:
        raw_dir = os.path.join(output_dir, "raw")
        ensure_dir(raw_dir)
        for i, dump in enumerate(dumps):
            filepath = os.path.join(raw_dir, f"sample_{i+1:03d}.txt")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(dump)

    # Analyze: parse all thread stacks and compute hot methods
    analysis = _analyze_stack_samples(dumps, thread_filter)

    # Save analysis results
    if output_dir:
        analysis_path = os.path.join(output_dir, "sampling_analysis.json")
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2, ensure_ascii=False)

    return analysis


def _parse_thread_dumps(text):
    """Parse a single jstack/jcmd Thread.print output into a dict of {thread_name: [stack_frames]}."""
    threads = {}
    current_name = None
    current_stack = []
    in_stack = False

    for line in text.splitlines():
        # Thread header: '"thread-name" #123 prio=5 ...'
        m = re.match(r'^"(.+?)"\s+#', line)
        if m:
            if current_name and current_stack:
                threads[current_name] = current_stack
            current_name = m.group(1)
            current_stack = []
            in_stack = False
            continue

        # Thread state line
        if "java.lang.Thread.State:" in line:
            in_stack = True
            continue

        # Stack frame: "at com.example.Service.method(File.java:123)"
        frame_m = re.match(r'^\s+at\s+(\S+)', line)
        if frame_m and in_stack:
            current_stack.append(frame_m.group(1))
            continue

        # Empty line or non-frame line ends this thread's stack
        if in_stack and not line.strip():
            if current_name and current_stack:
                threads[current_name] = current_stack
            current_name = None
            current_stack = []
            in_stack = False

    # Last thread
    if current_name and current_stack:
        threads[current_name] = current_stack

    return threads


def _is_ignored_thread(name):
    """Check if a thread should be ignored (JVM internals, system threads)."""
    for pattern in IGNORED_THREAD_PATTERNS:
        if re.match(pattern, name):
            return True
    return False


def _analyze_stack_samples(dumps, thread_filter=None):
    """
    Analyze multiple jstack dumps. For each business thread, count how often
    each unique stack top (or full stack) appears. Compute hot methods.

    Returns dict with:
      - total_samples: number of dumps analyzed
      - thread_hotspots: list of {thread_name, samples, hot_methods: [{method, count, pct}]}
      - overall_hot_methods: aggregated hot methods across all business threads
    """
    total_samples = len(dumps)

    # Collect per-thread stacks across all samples
    # {thread_name: [stack_from_sample1, stack_from_sample2, ...]}
    thread_stacks = {}
    for dump_text in dumps:
        threads = _parse_thread_dumps(dump_text)
        for name, stack in threads.items():
            if _is_ignored_thread(name):
                continue
            if thread_filter:
                if not re.search(thread_filter, name, re.IGNORECASE):
                    continue
            if name not in thread_stacks:
                thread_stacks[name] = []
            thread_stacks[name].append(stack)

    # Per-thread analysis: find hot methods
    thread_hotspots = []
    for name, stacks in sorted(thread_stacks.items(), key=lambda x: -len(x[1])):
        if len(stacks) < 2:
            # Thread appeared in <2 samples, not very informative
            continue

        # Count each unique stack frame occurrence
        method_counts = {}
        for stack in stacks:
            for frame in stack:
                # Shorten frame: "com.example.service.OrderService.createOrder(OrderService.java:45)"
                # -> "OrderService.createOrder" for readability
                short = _shorten_frame(frame)
                method_counts[short] = method_counts.get(short, 0) + 1

        # Also count unique "top of stack" (deepest user frame) occurrences
        # This is the most telling: where the thread is RIGHT NOW
        top_counts = {}
        for stack in stacks:
            top = _find_user_code_top(stack)
            if top:
                short = _shorten_frame(top)
                top_counts[short] = top_counts.get(short, 0) + 1

        # Sort by count descending
        hot_methods = sorted(method_counts.items(), key=lambda x: -x[1])[:15]
        hot_tops = sorted(top_counts.items(), key=lambda x: -x[1])[:10]

        thread_hotspots.append({
            "thread_name": name,
            "samples": len(stacks),
            "sample_pct": round(len(stacks) / total_samples * 100, 1),
            "hot_methods": [
                {"method": m, "count": c, "pct": round(c / len(stacks) * 100, 1)}
                for m, c in hot_methods
            ],
            "hot_stack_tops": [
                {"method": m, "count": c, "pct": round(c / len(stacks) * 100, 1)}
                for m, c in hot_tops
            ],
        })

    # Overall hot methods across all threads
    overall_counts = {}
    for th in thread_hotspots:
        for hm in th["hot_methods"]:
            overall_counts[hm["method"]] = overall_counts.get(hm["method"], 0) + hm["count"]
    overall_hot = sorted(overall_counts.items(), key=lambda x: -x[1])[:20]

    return {
        "total_samples": total_samples,
        "thread_hotspots": thread_hotspots,
        "overall_hot_methods": [
            {"method": m, "total_count": c} for m, c in overall_hot
        ],
    }


def _shorten_frame(frame):
    """Shorten a stack frame for readability.
    'com.example.service.OrderService.createOrder(OrderService.java:45)'
    -> 'OrderService.createOrder'
    """
    # Extract method part before '('
    m = re.match(r'(.+?)\(', frame)
    if not m:
        return frame
    full_method = m.group(1)
    # Get class.simpleName + methodName
    parts = full_method.rsplit(".", 1)
    if len(parts) == 2:
        class_name = parts[0].rsplit(".", 1)[-1]  # simple class name
        method_name = parts[1]
        return f"{class_name}.{method_name}"
    return full_method


def _find_user_code_top(stack):
    """Find the deepest user-code frame in a stack (skip JDK/spring framework internals)."""
    skip_prefixes = (
        "java.", "javax.", "sun.", "com.sun.", "jdk.",
        "org.springframework.", "org.apache.", "org.jboss.",
        "io.netty.", "reactor.", "org.eclipse.",
        "com.zaxxer.", "com.alibaba.druid.", "org.hibernate.",
        "org.mybatis.", "org.apache.ibatis.",
        "org.apache.catalina.", "org.apache.coyote.",
        "org.apache.tomcat.", "org.elasticsearch.",
    )
    # Walk from deepest (last) to shallowest, find first user code frame
    for frame in reversed(stack):
        m = re.match(r'(.+?)\(', frame)
        if not m:
            continue
        full = m.group(1)
        if any(full.startswith(p) for p in skip_prefixes):
            continue
        return frame
    # If no user code found, return the deepest frame
    return stack[-1] if stack else None


def _parse_trace_output(trace_text):
    """
    Parse Arthas trace output into a structured list of internal calls with timing.

    Arthas trace output format (example):
    `---[650ms] com.example.service.OrderService:createOrder()
        +---[0.05ms] com.example.service.OrderService:validate() #123
        +---[600ms] com.example.mapper.ItemMapper:selectByOrderId() #124
        +---[0.02ms] com.example.service.OrderService:assemble() #125
        `---[50ms] com.example.client.UserClient:getUser() #126

    Returns list of {method, avg_ms, pct} sorted by time descending.
    """
    calls = []
    # Match Arthas trace sub-call lines
    # Pattern: +---[123ms] or `---[123ms] followed by class:method()
    pattern = r'[+`]---\[(\d+(?:\.\d+)?)\s*ms\]\s+(\S+?)\(\)'
    total_ms = None

    for line in trace_text.splitlines():
        # First line: total method time
        m_total = re.match(r'`---\[(\d+(?:\.\d+)?)\s*ms\]\s+\S+', line)
        if m_total and total_ms is None:
            # Only set from the top-level entry
            # But this pattern also matches sub-calls, so check indentation
            if not line.startswith(' ') and not line.startswith('\t'):
                total_ms = float(m_total.group(1))

        # Sub-call lines
        m = re.search(pattern, line)
        if m:
            ms = float(m.group(1))
            method = m.group(2)
            # Shorten: "com.example.service.OrderService:validate" -> "OrderService.validate"
            if ":" in method:
                parts = method.rsplit(":", 1)
                class_short = parts[0].rsplit(".", 1)[-1]
                method = f"{class_short}.{parts[1]}"
            calls.append({"method": method, "ms": ms})

    if not calls:
        # Try alternate pattern for older Arthas versions
        pattern2 = r'[+`]---\[(\d+(?:\.\d+)?)\s*ms\]\s+(.+)'
        for line in trace_text.splitlines():
            m = re.search(pattern2, line)
            if m:
                ms = float(m.group(1))
                raw = m.group(2).strip().rstrip('#1234567890 ')
                if ms > 0.001:
                    calls.append({"method": raw, "ms": ms})

    # Calculate percentages
    total = sum(c["ms"] for c in calls) if calls else 1
    for c in calls:
        c["pct"] = round(c["ms"] / total * 100, 1) if total > 0 else 0
        c["avg_ms"] = c["ms"]

    calls.sort(key=lambda x: -x["ms"])
    return calls


def generate_sampling_report(analysis, pid, output_dir, env, trace_results=None):
    """Generate HTML report specifically for stack sampling results."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = analysis["total_samples"]

    # -- Summary --
    has_traces = trace_results and any("TRACE_FAILED" not in v for v in trace_results.values())
    summary_items = [
        f'<div class="metric"><span class="label">Samples</span><span class="value">{total}</span></div>',
        f'<div class="metric"><span class="label">Threads Analyzed</span><span class="value">{len(analysis["thread_hotspots"])}</span></div>',
    ]
    if has_traces:
        trace_count = sum(1 for v in trace_results.values() if "TRACE_FAILED" not in v)
        summary_items.append(f'<div class="metric"><span class="label">Methods Traced</span><span class="value">{trace_count}</span></div>')

    sections = [f"""
    <section>
        <h2>Sampling Summary</h2>
        <div class="summary-grid">{"".join(summary_items)}</div>
        <p style="margin-top:12px;color:#666">
            Collected {total} jstack snapshots. For each business thread, the methods that appear most frequently
            across samples are the bottlenecks — the thread is spending time there.
            <br><strong>Hot Stack Top</strong> = the deepest user-code method on the stack = exactly where the thread is stuck.
        </p>
    </section>
    """]

    # -- Overall Hot Methods --
    overall = analysis.get("overall_hot_methods", [])
    if overall:
        max_count = overall[0]["total_count"] if overall else 1
        rows = ""
        for i, om in enumerate(overall[:20]):
            bar_width = round(om["total_count"] / max_count * 100)
            bar_color = "#dc3545" if bar_width > 60 else ("#ffc107" if bar_width > 30 else "#28a745")
            rows += f'''<tr>
                <td>{i+1}</td>
                <td><code>{escape(om["method"])}</code></td>
                <td>{om["total_count"]}</td>
                <td><div style="background:{bar_color};height:18px;width:{bar_width}%;border-radius:3px"></div></td>
            </tr>'''
        sections.append(f"""
        <section>
            <h2>Overall Hot Methods (across all threads)</h2>
            <table>
                <tr><th>#</th><th>Method</th><th>Appearances</th><th>Frequency</th></tr>
                {rows}
            </table>
        </section>
        """)

    # -- Per-Thread Hotspots --
    for th in analysis["thread_hotspots"][:15]:
        name = th["thread_name"]
        samples = th["samples"]
        pct = th["sample_pct"]

        # Hot stack tops (most important — where thread is RIGHT NOW)
        hot_tops = th.get("hot_stack_tops", [])
        tops_html = ""
        if hot_tops:
            for ht in hot_tops:
                bar_w = ht["pct"]
                bar_color = "#dc3545" if bar_w > 60 else ("#ffc107" if bar_w > 30 else "#adb5bd")
                tops_html += f'''<tr>
                    <td><code>{escape(ht["method"])}</code></td>
                    <td>{ht["count"]}/{samples}</td>
                    <td>{ht["pct"]}%</td>
                    <td><div style="background:{bar_color};height:16px;width:{bar_w}%;border-radius:3px"></div></td>
                </tr>'''

        # Hot methods (all frames in stack)
        hot_methods = th.get("hot_methods", [])
        methods_html = ""
        for hm in hot_methods[:8]:
            methods_html += f'<tr><td><code>{escape(hm["method"])}</code></td><td>{hm["count"]}/{samples}</td><td>{hm["pct"]}%</td></tr>'

        section_html = f"""
        <section>
            <h2>{escape(name)} <span style="font-size:0.7em;color:#888">({samples}/{total} samples, {pct}%)</span></h2>
            <h3>Hot Stack Tops (where thread is stuck)</h3>
            <table>
                <tr><th>Method</th><th>Count</th><th>%</th><th>Bar</th></tr>
                {tops_html}
            </table>
            <details>
                <summary>All hot methods in stack</summary>
                <table>
                    <tr><th>Method</th><th>Count</th><th>%</th></tr>
                    {methods_html}
                </table>
            </details>
        </section>
        """
        sections.append(section_html)

    # ── Deep Trace Results ──
    if trace_results:
        trace_sections = ""
        for method, trace_text in trace_results.items():
            if "TRACE_FAILED" in trace_text:
                trace_sections += f"""
                <div class="trace-card">
                    <h3>{escape(method)}</h3>
                    <p style="color:#dc3545">Trace failed — Arthas could not attach or method not found</p>
                    <details><summary>Raw output</summary><pre>{escape(trace_text)}</pre></details>
                </div>
                """
                continue

            calls = _parse_trace_output(trace_text)
            if calls:
                max_ms = calls[0]["ms"] if calls else 1
                rows = ""
                for i, c in enumerate(calls):
                    bar_width = max(5, round(c["ms"] / max_ms * 100))
                    bar_color = "#dc3545" if c["pct"] > 60 else ("#ffc107" if c["pct"] > 20 else "#adb5bd")
                    rows += f"""<tr>
                        <td>{i+1}</td>
                        <td><code>{escape(c["method"])}</code></td>
                        <td>{c["avg_ms"]:.1f}ms</td>
                        <td>{c["pct"]}%</td>
                        <td><div style="background:{bar_color};height:16px;width:{bar_width}%;border-radius:3px"></div></td>
                    </tr>"""
                trace_sections += f"""
                <div class="trace-card">
                    <h3>{escape(method)} <span style="font-size:0.7em;color:#888">— internal call breakdown</span></h3>
                    <table>
                        <tr><th>#</th><th>Method</th><th>Avg Time</th><th>%</th><th>Bar</th></tr>
                        {rows}
                    </table>
                    <details>
                        <summary>Raw Arthas trace output</summary>
                        <pre>{escape(trace_text)}</pre>
                    </details>
                </div>
                """
            else:
                trace_sections += f"""
                <div class="trace-card">
                    <h3>{escape(method)}</h3>
                    <p style="color:#888">Trace captured but no sub-call timing parsed</p>
                    <details><summary>Raw output</summary><pre>{escape(trace_text)}</pre></details>
                </div>
                """

        if trace_sections:
            sections.append(f"""
            <section>
                <h2>Deep Trace: Hot Method Internal Breakdown</h2>
                <p style="margin-bottom:12px;color:#666">
                    For each hot method, Arthas trace shows the internal call tree with timing.
                    <br><strong>Red bars (>60%)</strong> = the specific internal call consuming most time = the real bottleneck.
                </p>
                <div class="trace-grid">{trace_sections}</div>
            </section>
            """)

    # Assemble HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stack Sampling Report — PID {pid}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; padding: 20px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #1a1a2e; margin-bottom: 5px; font-size: 1.6em; }}
  .timestamp {{ color: #666; margin-bottom: 20px; font-size: 0.9em; }}
  section {{ background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  h2 {{ color: #1a1a2e; margin-bottom: 12px; font-size: 1.2em; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
  h3 {{ color: #444; margin: 12px 0 8px; font-size: 1em; }}
  .summary-grid {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .metric {{ display: flex; flex-direction: column; background: #f8f9fa; border-radius: 6px; padding: 10px 16px; min-width: 120px; }}
  .metric .label {{ font-size: 0.75em; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
  .metric .value {{ font-size: 1.3em; font-weight: 600; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f8f9fa; font-weight: 600; color: #555; }}
  tr:hover {{ background: #f8f9fa; }}
  code {{ background: #f1f3f5; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; color: #d63384; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #0066cc; font-weight: 500; padding: 8px 0; }}
  details:hover summary {{ text-decoration: underline; }}
  .trace-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 12px; }}
  .trace-card {{ background: #f8f9fa; border-radius: 6px; padding: 16px; }}
  .trace-card h3 {{ margin: 0 0 10px; color: #1a1a2e; }}
</style>
</head>
<body>
<div class="container">
  <h1>Stack Sampling Report</h1>
  <p class="timestamp">PID {pid} &middot; {now} &middot; {escape(env["os"])} &middot; {total} samples</p>
  {"".join(sections)}
</div>
</body>
</html>"""

    report_path = os.path.join(output_dir, "sampling_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


def collect_service_checks(args):
    """Run all external service checks based on CLI args."""
    results = {}

    if args.check_redis:
        host, port = _parse_host_port(args.check_redis, 6379)
        log(f"Checking Redis at {host}:{port} ...")
        results["redis"] = check_redis(host, port, password=args.redis_password)

    if args.check_mysql:
        host, port = _parse_host_port(args.check_mysql, 3306)
        log(f"Checking MySQL at {host}:{port} ...")
        results["mysql"] = check_mysql(host, port)

    if args.check_pg:
        host, port = _parse_host_port(args.check_pg, 5432)
        log(f"Checking PostgreSQL at {host}:{port} ...")
        results["postgresql"] = check_postgresql(host, port)

    if args.actuator:
        log(f"Probing Actuator at {args.actuator} ...")
        results["actuator"] = check_actuator(args.actuator)

    return results


def _parse_host_port(s, default_port):
    parts = s.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else default_port
    return host, port


# ---------------------------------------------------------------------------
# Phase 5: Report Generation
# ---------------------------------------------------------------------------

def analyze_jdk_data(data):
    """Analyze collected JDK data and extract key findings."""
    findings = {
        "heap_usage_pct": None,
        "heap_status": "UNKNOWN",
        "deadlocks": [],
        "blocked_threads": [],
        "gc_full_gc_count": None,
        "gc_young_gc_count": None,
        "top_classes_by_instance": [],
        "jvm_flags_highlights": [],
        "spring_detected": False,
        "main_class": "",
    }

    # Parse heap info for usage percentage
    heap_text = data.get("heap_info", "")
    if heap_text:
        findings["heap_usage_pct"] = _parse_heap_usage(heap_text)
        if findings["heap_usage_pct"] is not None:
            if findings["heap_usage_pct"] >= HEAP_CRIT_PCT:
                findings["heap_status"] = "CRITICAL"
            elif findings["heap_usage_pct"] >= HEAP_WARN_PCT:
                findings["heap_status"] = "WARNING"
            else:
                findings["heap_status"] = "OK"

    # Parse thread dump for deadlocks and blocked threads
    td_text = data.get("threaddump", "")
    if td_text:
        findings["deadlocks"] = _parse_deadlocks(td_text)
        findings["blocked_threads"] = _parse_blocked_threads(td_text)

    # Parse GC stats
    gc_text = data.get("gc_stats", "")
    if gc_text:
        findings["gc_full_gc_count"] = _parse_gc_full_count(gc_text)
        findings["gc_young_gc_count"] = _parse_gc_young_count(gc_text)

    # Parse class histogram for top consumers
    hist_text = data.get("class_histogram", "")
    if hist_text:
        findings["top_classes_by_instance"] = _parse_class_histogram(hist_text, top_n=10)

    # Parse VM flags for notable settings
    flags_text = data.get("vm_flags", "")
    if flags_text:
        findings["jvm_flags_highlights"] = _parse_vm_flags(flags_text)

    # Parse command line
    cmdline_text = data.get("vm_command_line", "")
    if cmdline_text:
        findings["main_class"] = cmdline_text.strip().splitlines()[0][:200]
        findings["spring_detected"] = _is_spring_boot(cmdline_text)

    return findings


def _parse_heap_usage(text):
    """Extract heap usage percentage from jcmd GC.heap_info output."""
    # Look for patterns like "used = 512M" and "capacity = 1024M" in old gen
    # or "Old Generation" / "Eden Space" / "Survivor Space"
    used = None
    capacity = None
    # jcmd GC.heap_info format varies by GC; try multiple patterns
    # Pattern: "used      : 536870912" and "capacity  : 1073741824"
    for line in text.splitlines():
        line_l = line.lower().strip()
        # Try to find overall heap usage
        if "heap" in line_l and "used" in line_l and capacity is None:
            m = re.search(r"used\s*[:=]\s*(\d+)", line_l)
            if m:
                used = int(m.group(1))
        if "heap" in line_l and ("capacity" in line_l or "max" in line_l) and used is not None:
            m = re.search(r"(?:capacity|max)\s*[:=]\s*(\d+)", line_l)
            if m:
                capacity = int(m.group(1))
    if used is not None and capacity is not None and capacity > 0:
        return round(used / capacity * 100, 1)
    # Fallback: look for percentage in output
    m = re.search(r"(\d+(?:\.\d+)?)%", text)
    if m:
        return float(m.group(1))
    return None


def _parse_deadlocks(text):
    """Find deadlock info in thread dump."""
    deadlocks = []
    in_deadlock = False
    current = []
    for line in text.splitlines():
        if "DEADLOCK" in line.upper() or "found one java-level deadlock" in line.lower():
            in_deadlock = True
            current = [line]
        elif in_deadlock:
            current.append(line)
            if line.strip() == "" and len(current) > 2:
                deadlocks.append("\n".join(current))
                in_deadlock = False
                current = []
    if in_deadlock and current:
        deadlocks.append("\n".join(current))
    return deadlocks


def _parse_blocked_threads(text):
    """Find BLOCKED / WAITING threads with their stack traces."""
    blocked = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Match thread header lines like: "pool-1-thread-3" #123 prio=5 os_prio=0 tid=... java.lang.Thread.State: BLOCKED
        if "java.lang.Thread.State: BLOCKED" in line or "java.lang.Thread.State: WAITING" in line or "java.lang.Thread.State: TIMED_WAITING" in line:
            # Collect thread name from previous line
            thread_name = lines[i - 1].strip() if i > 0 else "unknown"
            state = "BLOCKED" if "BLOCKED" in line else ("WAITING" if "WAITING" in line and "TIMED" not in line else "TIMED_WAITING")
            stack = [line.strip()]
            j = i + 1
            while j < len(lines) and lines[j].startswith("\t"):
                stack.append(lines[j].strip())
                j += 1
            blocked.append({"name": thread_name, "state": state, "stack": stack[:5]})
            i = j
        else:
            i += 1
    return blocked[:20]  # Limit to top 20


def _parse_gc_full_count(text):
    """Extract Full GC count from jstat output."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    header = lines[0].split()
    last_line = lines[-1].split()
    for i, h in enumerate(header):
        if h in ("FGC", "fpc"):
            if i < len(last_line):
                try:
                    return int(float(last_line[i]))
                except (ValueError, IndexError):
                    pass
    return None


def _parse_gc_young_count(text):
    """Extract Young GC count from jstat output."""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None
    header = lines[0].split()
    last_line = lines[-1].split()
    for i, h in enumerate(header):
        if h in ("YGC", "ygc"):
            if i < len(last_line):
                try:
                    return int(float(last_line[i]))
                except (ValueError, IndexError):
                    pass
    return None


def _parse_class_histogram(text, top_n=10):
    """Parse jcmd GC.class_histogram output for top N classes by instance count."""
    results = []
    lines = text.strip().splitlines()
    for line in lines[1:]:
        parts = line.strip().split()
        if len(parts) >= 4 and parts[0].isdigit():
            try:
                instances = int(parts[1])
                size_kb = int(parts[2])
                class_name = parts[3]
                results.append({"class": class_name, "instances": instances, "size_kb": size_kb})
            except (ValueError, IndexError):
                continue
    # Sort by instance count descending
    results.sort(key=lambda x: x["instances"], reverse=True)
    return results[:top_n]


def _parse_vm_flags(text):
    """Extract notable JVM flags."""
    highlights = []
    notable_flags = {
        "HeapSize": r"MaxHeapSize\s*[:=]\s*(\d+)",
        "MetaspaceSize": r"MaxMetaspaceSize\s*[:=]\s*(\d+)",
        "UseG1GC": r"\bUseG1GC\b",
        "UseZGC": r"\bUseZGC\b",
        "UseCompressedOops": r"\bUseCompressedOops\b",
        "HeapDumpOnOutOfMemoryError": r"\bHeapDumpOnOutOfMemoryError\b",
    }
    for name, pattern in notable_flags.items():
        m = re.search(pattern, text)
        if m:
            value = m.group(1) if m.lastindex else "enabled"
            if name == "HeapSize" and value.isdigit():
                value = f"{int(value) // (1024*1024)} MB"
            if name == "MetaspaceSize" and value.isdigit():
                value = f"{int(value) // (1024*1024)} MB"
            highlights.append({"flag": name, "value": value})
    return highlights


def generate_html_report(data, findings, service_results, pid, output_dir, env):
    """Generate a unified HTML diagnostic report."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Severity color helpers
    def status_color(status):
        return {"CRITICAL": "#dc3545", "WARNING": "#ffc107", "OK": "#28a745", "UNKNOWN": "#6c757d"}.get(status, "#6c757d")

    def latency_color(ms, warn, crit):
        if ms is None:
            return "#6c757d"
        if ms >= crit:
            return "#dc3545"
        if ms >= warn:
            return "#ffc107"
        return "#28a745"

    # Build sections
    sections = []

    # -- Summary --
    heap_pct = findings.get("heap_usage_pct")
    heap_status = findings.get("heap_status", "UNKNOWN")
    deadlock_count = len(findings.get("deadlocks", []))
    blocked_count = len(findings.get("blocked_threads", []))

    summary_items = []
    if heap_pct is not None:
        summary_items.append(f'<div class="metric"><span class="label">Heap Usage</span><span class="value" style="color:{status_color(heap_status)}">{heap_pct}%</span></div>')
    summary_items.append(f'<div class="metric"><span class="label">Deadlocks</span><span class="value" style="color:{"#dc3545" if deadlock_count > 0 else "#28a745"}">{deadlock_count}</span></div>')
    summary_items.append(f'<div class="metric"><span class="label">Blocked Threads</span><span class="value" style="color:{"#ffc107" if blocked_count > 5 else "#28a745"}">{blocked_count}</span></div>')

    fgc = findings.get("gc_full_gc_count")
    if fgc is not None:
        fgc_color = "#dc3545" if fgc > 10 else ("#ffc107" if fgc > 3 else "#28a745")
        summary_items.append(f'<div class="metric"><span class="label">Full GC Count</span><span class="value" style="color:{fgc_color}">{fgc}</span></div>')

    ygc = findings.get("gc_young_gc_count")
    if ygc is not None:
        summary_items.append(f'<div class="metric"><span class="label">Young GC Count</span><span class="value">{ygc}</span></div>')

    sections.append(f"""
    <section>
        <h2>Summary</h2>
        <div class="summary-grid">{"".join(summary_items)}</div>
    </section>
    """)

    # -- JVM Info --
    main_class = findings.get("main_class", "N/A")
    spring = "Yes" if findings.get("spring_detected") else "No"
    flags = findings.get("jvm_flags_highlights", [])
    flags_html = "".join(f'<div class="metric"><span class="label">{escape(f["flag"])}</span><span class="value">{escape(str(f["value"]))}</span></div>' for f in flags)

    sections.append(f"""
    <section>
        <h2>JVM Information</h2>
        <div class="info-grid">
            <div class="metric"><span class="label">PID</span><span class="value">{pid}</span></div>
            <div class="metric"><span class="label">Main Class</span><span class="value" style="word-break:break-all">{escape(main_class)}</span></div>
            <div class="metric"><span class="label">Spring Boot</span><span class="value">{spring}</span></div>
            <div class="metric"><span class="label">OS</span><span class="value">{escape(env["os"])}</span></div>
        </div>
        <h3>Key JVM Flags</h3>
        <div class="info-grid">{flags_html}</div>
    </section>
    """)

    # -- Heap & Memory --
    heap_text = data.get("heap_info", "Not collected")
    sections.append(f"""
    <section>
        <h2>Heap & Memory</h2>
        <details>
            <summary>Raw heap info (click to expand)</summary>
            <pre>{escape(heap_text)}</pre>
        </details>
    </section>
    """)

    # -- Class Histogram --
    top_classes = findings.get("top_classes_by_instance", [])
    if top_classes:
        rows = ""
        for c in top_classes:
            rows += f'<tr><td>{escape(c["class"])}</td><td>{c["instances"]:,}</td><td>{c["size_kb"]:,} KB</td></tr>'
        sections.append(f"""
        <section>
            <h2>Top Classes by Instance Count</h2>
            <table>
                <tr><th>Class</th><th>Instances</th><th>Size</th></tr>
                {rows}
            </table>
        </section>
        """)

    # -- Deadlocks --
    deadlocks = findings.get("deadlocks", [])
    if deadlocks:
        dl_html = "".join(f'<pre class="deadlock">{escape(d)}</pre>' for d in deadlocks)
        sections.append(f"""
        <section>
            <h2 style="color:#dc3545">Deadlocks Detected!</h2>
            {dl_html}
        </section>
        """)

    # -- Blocked Threads --
    blocked = findings.get("blocked_threads", [])
    if blocked:
        bt_rows = ""
        for b in blocked[:10]:
            stack_html = "<br>".join(escape(s) for s in b["stack"][:3])
            bt_rows += f'<tr><td>{escape(b["name"][:60])}</td><td><span class="badge {"badge-blocked" if b["state"]=="BLOCKED" else "badge-waiting"}">{b["state"]}</span></td><td class="stack">{stack_html}</td></tr>'
        sections.append(f"""
        <section>
            <h2>Blocked / Waiting Threads (top 10)</h2>
            <table>
                <tr><th>Thread</th><th>State</th><th>Top Stack Frames</th></tr>
                {bt_rows}
            </table>
        </section>
        """)

    # -- GC Stats --
    gc_text = data.get("gc_stats", "Not collected")
    sections.append(f"""
    <section>
        <h2>GC Statistics</h2>
        <pre>{escape(gc_text)}</pre>
    </section>
    """)

    # -- Thread Dump (collapsible) --
    td_text = data.get("threaddump", "Not collected")
    sections.append(f"""
    <section>
        <h2>Full Thread Dump</h2>
        <details>
            <summary>Expand thread dump ({len(td_text.splitlines())} lines)</summary>
            <pre>{escape(td_text)}</pre>
        </details>
    </section>
    """)

    # -- Arthas Data (collapsible) --
    arthas_diag = data.get("arthas_diag", "")
    arthas_sql = data.get("arthas_sql", "")
    arthas_redis = data.get("arthas_redis", "")
    if arthas_diag or arthas_sql or arthas_redis:
        arthas_sections = ""
        if arthas_diag:
            arthas_sections += f'<details><summary>Arthas Diagnostic Output</summary><pre>{escape(arthas_diag)}</pre></details>'
        if arthas_sql:
            arthas_sections += f'<details><summary>SQL Trace</summary><pre>{escape(arthas_sql)}</pre></details>'
        if arthas_redis:
            arthas_sections += f'<details><summary>Redis Trace</summary><pre>{escape(arthas_redis)}</pre></details>'
        sections.append(f"""
        <section>
            <h2>Arthas Enhanced Diagnosis</h2>
            {arthas_sections}
        </section>
        """)

    # -- Service Checks --
    if service_results:
        svc_html = ""
        redis = service_results.get("redis")
        if redis:
            c_color = latency_color(redis.get("connect_ms"), DB_LATENCY_WARN_MS, DB_LATENCY_CRIT_MS)
            p_color = latency_color(redis.get("ping_ms"), REDIS_LATENCY_WARN_MS, REDIS_LATENCY_CRIT_MS)
            svc_html += f"""
            <div class="service-card">
                <h3>Redis ({escape(redis['host'])}:{redis['port']})</h3>
                <div class="metric"><span class="label">Status</span><span class="value" style="color:{"#28a745" if redis["status"]=="OK" else "#dc3545"}">{redis["status"]}</span></div>
                <div class="metric"><span class="label">Connect</span><span class="value" style="color:{c_color}">{redis.get("connect_ms", "N/A")} ms</span></div>
                <div class="metric"><span class="label">PING</span><span class="value" style="color:{p_color}">{redis.get("ping_ms", "N/A")} ms</span></div>
            </div>
            """

        mysql = service_results.get("mysql")
        if mysql:
            c_color = latency_color(mysql.get("connect_ms"), DB_LATENCY_WARN_MS, DB_LATENCY_CRIT_MS)
            version = mysql.get("server_version", "")
            svc_html += f"""
            <div class="service-card">
                <h3>MySQL ({escape(mysql['host'])}:{mysql['port']})</h3>
                <div class="metric"><span class="label">Status</span><span class="value" style="color:{"#28a745" if mysql["status"]=="OK" else "#dc3545"}">{mysql["status"]}</span></div>
                <div class="metric"><span class="label">Connect</span><span class="value" style="color:{c_color}">{mysql.get("connect_ms", "N/A")} ms</span></div>
                {"<div class='metric'><span class='label'>Version</span><span class='value'>" + escape(version) + "</span></div>" if version else ""}
            </div>
            """

        pg = service_results.get("postgresql")
        if pg:
            c_color = latency_color(pg.get("connect_ms"), DB_LATENCY_WARN_MS, DB_LATENCY_CRIT_MS)
            svc_html += f"""
            <div class="service-card">
                <h3>PostgreSQL ({escape(pg['host'])}:{pg['port']})</h3>
                <div class="metric"><span class="label">Status</span><span class="value" style="color:{"#28a745" if pg["status"]=="OK" else "#dc3545"}">{pg["status"]}</span></div>
                <div class="metric"><span class="label">Connect</span><span class="value" style="color:{c_color}">{pg.get("connect_ms", "N/A")} ms</span></div>
            </div>
            """

        actuator = service_results.get("actuator")
        if actuator:
            ep_rows = ""
            for ep_name, ep_info in actuator.get("endpoints", {}).items():
                status = ep_info.get("status", "UNKNOWN")
                s_color = "#28a745" if status == "OK" else "#6c757d"
                latency = ep_info.get("latency_ms")
                latency_str = f'{latency} ms' if latency is not None else "N/A"
                ep_rows += f'<tr><td>{escape(ep_name)}</td><td style="color:{s_color}">{status}</td><td>{latency_str}</td></tr>'
            svc_html += f"""
            <div class="service-card">
                <h3>Spring Boot Actuator</h3>
                <table>
                    <tr><th>Endpoint</th><th>Status</th><th>Latency</th></tr>
                    {ep_rows}
                </table>
            </div>
            """

        if svc_html:
            sections.append(f'<section><h2>External Service Checks</h2><div class="service-grid">{svc_html}</div></section>')

    # -- Assemble full HTML --
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Java Diagnostic Report — PID {pid}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; padding: 20px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #1a1a2e; margin-bottom: 5px; font-size: 1.6em; }}
  .timestamp {{ color: #666; margin-bottom: 20px; font-size: 0.9em; }}
  section {{ background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  h2 {{ color: #1a1a2e; margin-bottom: 12px; font-size: 1.2em; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
  h3 {{ color: #444; margin: 12px 0 8px; font-size: 1em; }}
  .summary-grid, .info-grid {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .metric {{ display: flex; flex-direction: column; background: #f8f9fa; border-radius: 6px; padding: 10px 16px; min-width: 120px; }}
  .metric .label {{ font-size: 0.75em; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
  .metric .value {{ font-size: 1.3em; font-weight: 600; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85em; }}
  th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
  th {{ background: #f8f9fa; font-weight: 600; color: #555; }}
  tr:hover {{ background: #f8f9fa; }}
  pre {{ background: #1e1e2e; color: #cdd6f4; padding: 16px; border-radius: 6px; overflow-x: auto; font-size: 0.8em; line-height: 1.5; max-height: 500px; overflow-y: auto; }}
  .deadlock {{ border: 2px solid #dc3545; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #0066cc; font-weight: 500; padding: 8px 0; }}
  details:hover summary {{ text-decoration: underline; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 600; color: #fff; }}
  .badge-blocked {{ background: #dc3545; }}
  .badge-waiting {{ background: #ffc107; color: #333; }}
  .stack {{ font-family: monospace; font-size: 0.85em; color: #666; }}
  .service-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
  .service-card {{ background: #f8f9fa; border-radius: 6px; padding: 16px; }}
  .service-card h3 {{ margin: 0 0 10px; color: #1a1a2e; }}
  @media (max-width: 600px) {{
    .summary-grid, .info-grid {{ flex-direction: column; }}
    .metric {{ min-width: 100%; }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>Java Diagnostic Report</h1>
  <p class="timestamp">PID {pid} &middot; {now} &middot; {escape(env["os"])} &middot; java-diag.py v{VERSION}</p>
  {"".join(sections)}
</div>
</body>
</html>"""

    report_path = os.path.join(output_dir, "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Java Performance Diagnostic Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python java-diag.py                         # List Java processes
  python java-diag.py -p 12345                # Diagnose PID 12345
  python java-diag.py -p 12345 --full         # Full diagnosis (Arthas + heap dump)
  python java-diag.py -p 12345 --lite         # Lite diagnosis (JDK tools only)
  python java-diag.py --check-redis localhost:6379
  python java-diag.py -p 12345 --actuator http://localhost:8080/actuator
"""
    )
    parser.add_argument("-p", "--pid", type=int, help="Java process PID to diagnose")
    parser.add_argument("--full", action="store_true", help="Full mode: Arthas + heap dump + service checks")
    parser.add_argument("--lite", action="store_true", help="Lite mode: JDK tools only, no Arthas, no heap dump")
    parser.add_argument("--check-redis", metavar="HOST:PORT", help="Check Redis connectivity (default port: 6379)")
    parser.add_argument("--redis-password", metavar="PWD", help="Redis AUTH password")
    parser.add_argument("--check-mysql", metavar="HOST:PORT", help="Check MySQL connectivity (default port: 3306)")
    parser.add_argument("--check-pg", metavar="HOST:PORT", help="Check PostgreSQL connectivity (default port: 5432)")
    parser.add_argument("--actuator", metavar="URL", help="Spring Boot Actuator base URL (e.g. http://localhost:8080/actuator)")
    parser.add_argument("-o", "--output", metavar="DIR", help="Output directory (default: ./diag-report-TIMESTAMP)")
    parser.add_argument("--no-arthas", action="store_true", help="Skip Arthas even in --full mode")
    parser.add_argument("-v", "--version", action="version", version=f"java-diag.py {VERSION}")

    # Sampling mode
    sample_group = parser.add_argument_group("Stack Sampling Mode")
    sample_group.add_argument("--sample", action="store_true",
        help="Stack sampling mode: take multiple jstack snapshots while app is slow, analyze hot methods")
    sample_group.add_argument("--sample-count", type=int, default=10, metavar="N",
        help="Number of jstack snapshots to take (default: 10)")
    sample_group.add_argument("--sample-interval", type=float, default=1.0, metavar="SEC",
        help="Seconds between snapshots (default: 1.0)")
    sample_group.add_argument("--thread-filter", metavar="REGEX",
        help="Only analyze threads whose name matches this regex (e.g. 'http-nio|pool-|batch')")
    sample_group.add_argument("--trace-count", type=int, default=5, metavar="N",
        help="Number of Arthas trace captures per hot method (default: 5)")
    sample_group.add_argument("--no-trace", action="store_true",
        help="Skip Arthas deep tracing of hot methods after sampling")

    # Offline / intranet
    parser.add_argument("--arthas-jar", metavar="PATH",
        help="Path to local arthas-boot.jar (for intranet/offline environments)")
    parser.add_argument("--offline", action="store_true",
        help="Offline mode: skip all network downloads and external service checks")
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"\n{'='*60}")
    print(f"  Java Performance Diagnostic Tool v{VERSION}")
    print(f"  {'='*56}\n")

    # Phase 1: Environment Discovery
    log("Phase 1: Environment Discovery")
    log("-" * 40)
    env = detect_environment()
    if not env["jdk_tools"].get("jcmd") and not env["jdk_tools"].get("jps"):
        log("No JDK diagnostic tools found. Is JAVA_HOME set and JDK installed?", "ERROR")
        sys.exit(1)

    # List Java processes
    processes = list_java_processes(env)
    if not processes:
        log("No Java processes found.", "ERROR")
        sys.exit(1)

    print(f"\n  {'PID':<8} {'Spring':<8} {'Main Class'}")
    print(f"  {'-'*7:<8} {'-'*6:<8} {'-'*50}")
    for p in processes:
        spring_mark = "Yes" if p["is_spring"] else ""
        print(f"  {p['pid']:<8} {spring_mark:<8} {p['main_class'][:60]}")
    print()

    # Service checks only (no PID required)
    if not args.offline and (args.check_redis or args.check_mysql or args.check_pg or args.actuator):
        if not args.pid:
            service_results = collect_service_checks(args)
            output_dir = args.output or f"{REPORT_DIR_PREFIX}{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            ensure_dir(output_dir)
            # Generate minimal report for service checks
            findings = {}
            data = {}
            report_path = generate_html_report(data, findings, service_results, "N/A", output_dir, env)
            log(f"\nService check report: {report_path}", "OK")
            return

    # Select PID
    pid = args.pid
    if not pid:
        try:
            pid_input = input("Enter PID to diagnose: ").strip()
            pid = int(pid_input)
        except (ValueError, EOFError):
            log("Invalid PID", "ERROR")
            sys.exit(1)

    # Verify PID is a Java process
    pid_valid = any(p["pid"] == pid for p in processes)
    if not pid_valid:
        log(f"PID {pid} is not a known Java process. Proceeding anyway...", "WARN")

    # ── Sampling Mode ──
    if args.sample:
        output_dir = args.output or f"{REPORT_DIR_PREFIX}{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        ensure_dir(output_dir)
        log(f"Output directory: {output_dir}")

        analysis = run_stack_sampling(
            pid, env,
            count=args.sample_count,
            interval=args.sample_interval,
            thread_filter=args.thread_filter,
            output_dir=output_dir,
        )
        if analysis:
            # Print console summary
            print(f"\n  === SAMPLING RESULTS ===\n")
            print(f"  Samples collected: {analysis['total_samples']}")
            print(f"  Business threads found: {len(analysis['thread_hotspots'])}")

            # Print top 5 hot stack tops
            for th in analysis["thread_hotspots"][:5]:
                print(f"\n  Thread: {th['thread_name']} ({th['samples']}/{analysis['total_samples']} samples, {th['sample_pct']}%)")
                if th.get("hot_stack_tops"):
                    print(f"    Hot Stack Tops:")
                    for ht in th["hot_stack_tops"][:3]:
                        print(f"      {ht['pct']:5.1f}%  {ht['method']}")

            # Print overall hot methods
            overall = analysis.get("overall_hot_methods", [])
            if overall:
                print(f"\n  Overall Top Methods:")
                for om in overall[:5]:
                    print(f"    {om['total_count']:4d}x  {om['method']}")

            # ── Deep Trace: auto-trace top hot methods via Arthas ──
            trace_results = {}
            if not args.no_trace and not args.no_arthas and not args.offline and overall:
                print(f"\n{'='*60}")
                log("Phase 2.5b: Deep Tracing Hot Methods")
                log("-" * 40)
                log("Auto-tracing top hot methods to break down internal costs ...")
                top_methods = [om["method"] for om in overall[:5]]
                trace_results = trace_hot_methods(
                    pid, env, top_methods, output_dir,
                    local_jar=args.arthas_jar,
                    trace_count=args.trace_count if hasattr(args, 'trace_count') else 5,
                )
                if trace_results:
                    # Parse and print trace summary
                    for method, trace_text in trace_results.items():
                        if "TRACE_FAILED" in trace_text:
                            continue
                        summary = _parse_trace_output(trace_text)
                        if summary:
                            print(f"\n  [{method}] Internal breakdown:")
                            for call in summary[:5]:
                                bar = "#" * max(1, int(call["pct"] / 5))
                                print(f"    {call['pct']:5.1f}% {bar} {call['method']} ({call['avg_ms']:.0f}ms avg)")

            report_path = generate_sampling_report(analysis, pid, output_dir, env, trace_results=trace_results)
            log(f"\nSampling Report: {report_path}", "OK")
            log(f"Raw Samples:     {os.path.join(output_dir, 'raw')}")
            print(f"\n{'='*60}")
            print(f"  Open sampling_report.html in a browser for full analysis.")
            print(f"  Report: {report_path}")
            print(f"{'='*60}\n")
        return

    # Create output directory
    output_dir = args.output or f"{REPORT_DIR_PREFIX}{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    ensure_dir(output_dir)
    log(f"Output directory: {output_dir}")

    # Phase 2: JDK Tool Data Collection
    print(f"\n{'='*60}")
    log("Phase 2: JDK Tool Data Collection")
    log("-" * 40)
    lite_mode = args.lite
    data = collect_jdk_data(pid, env, output_dir, lite=lite_mode)

    # Phase 3: Arthas Enhanced Collection
    arthas_data = {}
    if args.full and not args.no_arthas and not lite_mode:
        print(f"\n{'='*60}")
        log("Phase 3: Arthas Enhanced Collection")
        log("-" * 40)
        arthas_data = collect_arthas_data(pid, env, output_dir, local_jar=args.arthas_jar)
    else:
        log("Skipping Arthas (use --full to enable)")

    data.update(arthas_data)

    # Phase 4: External Service Checks
    service_results = {}
    if not args.offline and (args.full or args.check_redis or args.check_mysql or args.check_pg or args.actuator):
        print(f"\n{'='*60}")
        log("Phase 4: External Service Checks")
        log("-" * 40)
        service_results = collect_service_checks(args)

    # Phase 5: Analysis & Report Generation
    print(f"\n{'='*60}")
    log("Phase 5: Analysis & Report Generation")
    log("-" * 40)
    findings = analyze_jdk_data(data)

    # Print summary to console
    print(f"\n  === DIAGNOSTIC SUMMARY ===\n")
    heap_pct = findings.get("heap_usage_pct")
    if heap_pct is not None:
        color_code = {"CRITICAL": "\033[91m", "WARNING": "\033[93m", "OK": "\033[92m"}.get(findings["heap_status"], "")
        reset = "\033[0m"
        print(f"  Heap Usage:   {color_code}{heap_pct}%{reset} ({findings['heap_status']})")
    else:
        print(f"  Heap Usage:   Not available")

    dl = findings.get("deadlocks", [])
    print(f"  Deadlocks:    {len(dl)} {'!!! FOUND !!!' if dl else ''}")
    print(f"  Blocked/WAIT: {len(findings.get('blocked_threads', []))} threads")
    if findings.get("gc_full_gc_count") is not None:
        print(f"  Full GC:      {findings['gc_full_gc_count']}")
    if findings.get("gc_young_gc_count") is not None:
        print(f"  Young GC:     {findings['gc_young_gc_count']}")

    for svc_name, svc_info in service_results.items():
        if isinstance(svc_info, dict) and "status" in svc_info:
            status = svc_info["status"]
            extra = ""
            if svc_info.get("connect_ms") is not None:
                extra = f" ({svc_info['connect_ms']}ms)"
            elif svc_info.get("ping_ms") is not None:
                extra = f" (ping: {svc_info['ping_ms']}ms)"
            print(f"  {svc_name.capitalize()}:   {status}{extra}")

    # Generate HTML report
    report_path = generate_html_report(data, findings, service_results, pid, output_dir, env)
    log(f"\nHTML Report: {report_path}", "OK")
    log(f"Raw Data:    {os.path.join(output_dir, 'raw')}")
    print(f"\n{'='*60}")
    print(f"  Diagnosis complete. Open the HTML report in a browser.")
    print(f"  Report: {report_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
