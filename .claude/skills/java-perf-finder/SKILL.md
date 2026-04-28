---
name: java-perf-finder
description: >
  Java performance bottleneck locator. Analyze code to precisely identify slow code — slow SQL,
  N+1 queries, memory leaks, thread blocking, inefficient loops, and more. Use when: (1) User says
  Java code/app/API is slow, (2) Need to find performance bottlenecks in Java projects,
  (3) User mentions 性能/慢/卡/超时/OOM/高CPU in Java context,
  (4) User asks to optimize or profile Java code.
  Supports Spring, MyBatis, and general Java projects.
---

# Java Performance Finder

Precisely locate slow Java code through multi-dimensional analysis.

## Workflow

```
1. Understand symptom → 2. Static code scan → 2.5. Runtime auto-diagnosis → 3. Runtime diagnosis guide → 4. Comprehensive report
```

### Step 1: Understand Symptom

Ask the user:
- **What is slow?** API response, batch job, startup, memory growing, CPU high
- **Where?** Specific module/class, or unknown
- **How slow?** Response time, frequency (always slow / occasional)

If user already described the problem, skip and proceed.

Based on the symptom, select analysis focus:

| Symptom | Primary Analysis Focus |
|---------|----------------------|
| API slow | SQL, N+1, serialization, blocking calls |
| Batch/job slow | Loop efficiency, batch size, IO, DB bulk ops |
| Startup slow | Bean init, auto-configuration, classpath scan |
| OOM / memory growth | Object leaks, collection growth, cache unbounded |
| CPU high | Hot loops, regex, serialization, GC overhead |
| Thread deadlock/block | Lock contention, synchronized scope, thread pool saturation |

### Step 2: Static Code Scan

Read the relevant Java source files. Scan for the following anti-patterns, organized by severity.

#### Critical (almost always the root cause)

1. **N+1 Query** — Loop body contains DB call (mapper/select invocation inside for/while/forEach)
2. **Missing Index / Full Table Scan** — Query conditions not matching indexes
3. **Synchronous Blocking in Async Context** — HTTP/RPC/DB call blocking the main thread
4. **Unbounded Collection Growth** — Maps/Lists that never get cleared
5. **Large Object in Loop** — Creating big objects repeatedly (byte[], String concat, DOM)

#### High (frequently causes slowness)

6. **Wide Transaction Scope** — `@Transactional` wrapping slow non-DB operations
7. **Missing Pagination** — Loading all records at once
8. **Inefficient Serialization** — Repeatedly serializing/deserializing same data
9. **Coarse Lock Scope** — `synchronized` on entire method when only partial state needs guarding
10. **Thread Pool Misconfiguration** — Too few threads, unbounded queue, or rejected execution

#### Medium (context-dependent impact)

11. **Repeated Reflection** — `Class.forName()`, `Method.invoke()` in hot path
12. **Regex compilation in loop** — `Pattern.compile()` called repeatedly instead of pre-compiling
13. **Unnecessary auto-boxing** — `Integer` vs `int` in tight loops
14. **Deep call stack for simple logic** — over-abstracted layers adding proxy/AOP overhead

See [references/anti-patterns.md](references/anti-patterns.md) for detailed pattern descriptions with code examples and grep patterns.

### Step 2.5: Automated Runtime Diagnosis

#### Option A: Stack Sampling Mode (MOST PRECISE — use this first)

This is the most direct way to find slow code. Ask the user to reproduce the slowness while the script samples thread stacks, then analyze which methods the business threads are stuck on most often.

```bash
# 1. Ask user: which API/operation is slow?
# 2. Start sampling — ask user to trigger the slow operation NOW
python scripts/java-diag.py -p 12345 --sample

# Customize sampling
python scripts/java-diag.py -p 12345 --sample --sample-count 20 --sample-interval 0.5

# Filter to specific threads only (e.g. HTTP handler threads)
python scripts/java-diag.py -p 12345 --sample --thread-filter "http-nio|pool-|batch"
```

**How it works:**
1. Script takes N jstack snapshots (default 10, every 1 second)
2. User triggers the slow operation during sampling (call the API, click the button)
3. Script parses all thread stacks, counts how often each method appears
4. **Hot Stack Top** = the deepest user-code method on the stack = where the thread is RIGHT NOW
5. A method appearing in 8/10 samples = 80% of the time the thread is stuck there = **the bottleneck**

**Reading the report:**
- `sampling_report.html` — per-thread hot methods with bar charts
- Focus on **Hot Stack Tops** — this is where the thread is spending time
- A method with >60% appearance rate = almost certainly the bottleneck
- Multiple threads stuck on the same method = systemic bottleneck

#### Option B: Full Diagnostic Mode (comprehensive health check)

```bash
# List Java processes and pick one
python scripts/java-diag.py

# Diagnose specific PID (lite mode — JDK tools only)
python scripts/java-diag.py -p 12345 --lite

# Full mode — JDK tools + Arthas + heap dump + external services
python scripts/java-diag.py -p 12345 --full

# Check external services only
python scripts/java-diag.py --check-redis localhost:6379 --check-mysql localhost:3306
```

**What the full script collects:**
- Phase 1: Environment discovery (OS, JAVA_HOME, JDK tools, Java process list, Spring Boot detection)
- Phase 2: JDK tool data (thread dump, heap info, class histogram, GC stats, VM flags)
- Phase 3: Arthas enhanced data (dashboard, top CPU threads, deadlock detection, SQL/Redis tracing)
- Phase 4: External service checks (Redis PING latency, MySQL/PG connectivity, Spring Boot Actuator)
- Phase 5: HTML report with color-coded severity (red/yellow/green)

Output: `diag-report-YYYYMMDD-HHMMSS/report.html` + raw data in `raw/` subdirectory.

**Requirements:** Python 3.6+ (zero required dependencies). Optional: `psutil` for richer process listing. Arthas is auto-downloaded in `--full` mode.

If the process is on a remote server, copy the script there and run it. If SSH access is not available, fall back to manual commands from Step 3.

Review the generated HTML report to confirm or refine findings from Step 2. Key things to look for:
- **Heap > 90%** — memory pressure, possible leak
- **Deadlocks detected** — immediate fix required
- **Many BLOCKED threads** — lock contention
- **High Full GC count** — memory or allocation issue
- **Top classes by instance count** — potential memory leak suspects
- **Slow Redis/DB connectivity** — network or connection pool issue

### Step 3: Runtime Diagnosis Guide

For each suspected bottleneck found in Step 2, provide corresponding runtime verification commands.

#### Arthas Commands (most versatile)

```bash
# Method execution time
trace com.example.service.OrderService createOrder -n 5

# Watch method params and return
watch com.example.service.OrderService createOrder '{params, returnObj}' -n 3 -x 3

# Top busy threads
thread -n 5

# Dashboard overview
dashboard
```

#### SQL Analysis

```bash
# Enable MyBatis SQL logging
logging.level.com.example.mapper=DEBUG

# Arthas — monitor mapper call frequency and time
trace com.example.mapper.* * -n 10

# MySQL slow query log
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 1;
```

#### Memory Analysis

```bash
# Heap dump on OOM
-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp/heap.hprof
```

See [references/runtime-tools.md](references/runtime-tools.md) for more tool-specific commands and scenarios.

### Step 4: Comprehensive Report

Present findings in this format:

```
## Performance Analysis Report

### Symptom
<User-described problem>

### Findings (by severity)

#### [CRITICAL] N+1 Query — OrderService.java:45
```java
for (Order order : orders) {
    order.setItems(itemMapper.selectByOrderId(order.getId())); // N+1
}
```
Impact: 1000 orders → 1001 DB queries
Fix: Use batch query `itemMapper.selectByOrderIds(orderIds)`

#### [HIGH] Missing Pagination — OrderMapper.xml:23
...
```

**Report rules:**
- Always show exact file path and line number
- Always show the problematic code snippet
- Always explain WHY it's slow (the mechanism)
- Always provide a concrete fix, not just "optimize this"
- Order by impact (biggest performance gain first)
