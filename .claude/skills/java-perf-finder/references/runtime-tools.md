# Runtime Diagnosis Tools

## Table of Contents
- [Arthas](#arthas)
- [JDK Built-in Tools](#jdk-built-in-tools)
- [MySQL Diagnosis](#mysql-diagnosis)
- [Redis Diagnosis](#redis-diagnosis)
- [GC Analysis](#gc-analysis)

## Automated Diagnosis Script

The bundled `scripts/java-diag.py` automates data collection across all tools below. See SKILL.md Step 2.5 for usage.

### Interpreting Script Output

**HTML Report (`report.html`)** — Open in browser, check these first:
- **Summary section**: Heap usage % (red > 90%, yellow > 70%), deadlock count, blocked thread count, Full GC count
- **Deadlocks section**: If present, this is an urgent fix — threads are permanently blocked
- **Blocked/Waiting Threads**: Shows thread name, state (BLOCKED/WAITING), and top stack frames. Many BLOCKED threads → lock contention
- **Top Classes by Instance Count**: Classes with very high instance counts are memory leak suspects (e.g., HashMap$Node, byte[], String with millions of instances)
- **GC Statistics**: Compare YGC (young GC) and FGC (full GC) counts. High FGC indicates memory pressure
- **Service Checks**: Redis PING latency, MySQL connect time, Actuator endpoint status

**Raw Data (`raw/` directory)** — For deep analysis:
- `threaddump.txt` — Full thread dump, search for BLOCKED, WAITING, parking
- `heap_info.txt` — Heap generation layout and usage
- `class_histogram.txt` — All classes ranked by instance count/size
- `gc_stats.txt` — jstat GC snapshots (3 samples, 1s apart)
- `vm_flags.txt` — All JVM flags and values
- `arthas_diag.txt` — Arthas dashboard + thread + memory + JVM info
- `arthas_sql.txt` — Arthas JDBC/MyBatis/Druid tracing results
- `arthas_redis.txt` — Arthas Redis tracing results
- `heap.hprof` — Full heap dump (only in `--full` mode), analyze with MAT or VisualVM

---

## Arthas

### Install & Attach
```bash
# Download
curl -O https://arthas.aliyun.com/arthas-boot.jar

# Attach to running Java process
java -jar arthas-boot.jar <pid>
```

### Method Tracing
```bash
# Trace single method, show sub-call times
trace com.example.service.OrderService createOrder

# Trace with condition — only trace when cost > 100ms
trace com.example.service.OrderService createOrder '#cost > 100'

# Trace all methods in a class
trace com.example.service.OrderService *

# Trace mapper layer to find slow SQL
trace com.example.mapper.* *

# Trace with depth limit (default 4, max 10)
trace com.example.service.OrderService createOrder -n 5
```

### Method Monitoring
```bash
# Monitor call count / success rate / avg time per 5s
monitor com.example.service.OrderService createOrder -c 5

# Watch input params and return value
watch com.example.service.OrderService createOrder '{params, returnObj}' -n 3 -x 3

# Watch exception
watch com.example.service.OrderService createOrder '{params, throwExp}' -e -n 3
```

### Thread Analysis
```bash
# Top 5 busiest threads
thread -n 5

# Find thread by state
thread -all | grep BLOCKED

# Thread stack for specific thread
thread <thread-id>

# Deadlock detection
thread -b
```

### Memory Analysis
```bash
# Heap dump
heapdump /tmp/dump.hprof

# Check class instance count
sc -d com.example.model.Order

# View OGNL expression for object fields
ognl '@com.example.Config@cache.size()'
```

### Flame Graph
```bash
# Generate flame graph (profiler)
profiler start
# ... wait 30s ...
profiler stop --format html
# Downloads flamegraph.html
```

---

## JDK Built-in Tools

### jps — List Java Processes
```bash
jps -lv
```

### jstack — Thread Dump
```bash
# Capture thread dump
jstack <pid> > thread_dump.txt

# Force thread dump on unresponsive JVM
jstack -F <pid> > thread_dump.txt

# Take 3 dumps 5s apart to find persistent BLOCKED threads
for i in 1 2 3; do jstack <pid> > dump_$i.txt; sleep 5; done
```

### jmap — Heap Analysis
```bash
# Heap histogram — top memory-consuming classes
jmap -histo <pid> | head -30

# Heap dump
jmap -dump:format=b,file=/tmp/heap.hprof <pid>
```

### jstat — GC Statistics
```bash
# GC summary, every 1s
jstat -gc <pid> 1000

# Key columns: YGC (young GC count), YGCT (young GC time), FGC (full GC count), FGCT (full GC time)
# High FGC frequency → memory pressure
```

### JCMD (Java 8+)
```bash
# JVM info
jcmd <pid> VM.info

# GC heap info
jcmd <pid> GC.heap_info

# Thread dump
jcmd <pid> Thread.print
```

---

## MySQL Diagnosis

### Slow Query Log
```sql
-- Enable slow query log
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 1;  -- seconds
SET GLOBAL log_queries_not_using_indexes = 'ON';

-- Check slow queries
SHOW VARIABLES LIKE 'slow_query_log_file';
```

### Explain Analysis
```sql
-- Always EXPLAIN before optimizing
EXPLAIN SELECT * FROM t_order WHERE status = 1 AND create_time > '2024-01-01';

-- Key columns:
-- type: ALL (full scan) → bad; ref/eq_ref/range → good
-- rows: estimated scanned rows
-- Extra: Using filesort, Using temporary → bad
```

### Current Running Queries
```sql
SHOW PROCESSLIST;
-- or for full info:
SHOW FULL PROCESSLIST;

-- Kill a slow query
KILL <id>;
```

### InnoDB Status
```sql
SHOW ENGINE INNODB STATUS\G
-- Check: TRANSACTIONS section for long-running transactions, lock waits
```

### Index Usage Stats
```sql
-- Find unused indexes
SELECT * FROM sys.schema_unused_indexes;

-- Find redundant indexes
SELECT * FROM sys.schema_redundant_indexes;
```

---

## Redis Diagnosis

### Latency Check
```bash
# Redis built-in latency monitor
CONFIG SET latency-monitor-threshold 100  # 100ms

# Check recent slow commands
SLOWLOG GET 10

# Latency history
LATENCY HISTORY command
LATENCY HISTORY fork

# Runtime stats
INFO commandstats
```

### Connection & Memory
```bash
INFO clients        # connected clients
INFO memory         # memory usage, peak
INFO stats          # keyspace hits/misses ratio
```

---

## GC Analysis

### Common GC Issues by Symptom

| Symptom | Likely Cause | JVM Flags to Add |
|---------|-------------|-----------------|
| Frequent Full GC | Old gen filling up, memory leak | `-Xlog:gc*` (Java 9+) or `-XX:+PrintGCDetails` |
| Long GC pauses (>500ms) | Heap too large, or CMS degradation | Try G1: `-XX:+UseG1GC` |
| GC overhead limit | >98% time in GC, <2% recovered | Check for memory leak, increase heap |
| Metaspace OOM | Classloader leak | `-XX:MaxMetaspaceSize=256m -XX:+TraceClassLoading` |

### Recommended GC Flags for Diagnosis
```bash
# Java 8
-XX:+PrintGCDetails -XX:+PrintGCDateStamps -XX:+PrintGCTimeStamps
-Xloggc:/tmp/gc.log -XX:+UseGCLogFileRotation -XX:NumberOfGCLogFiles=5 -XX:GCLogFileSize=20M

# Java 9+
-Xlog:gc*=info:file=/tmp/gc.log:time,uptime:filecount=5,filesize=20M
```
