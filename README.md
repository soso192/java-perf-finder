# Java Perf Finder / Java 性能瓶颈定位器

[English](#english) | [中文](#中文)

---

<a id="english"></a>

## Java Perf Finder

A Claude Code skill that precisely identifies performance bottlenecks in Java projects — slow SQL, N+1 queries, memory leaks, thread blocking, inefficient loops, and more. Designed for Spring Boot, MyBatis, and general Java applications.

### Features

- **Symptom Classification** — Maps observed symptoms (API slow, batch/job slow, startup slow, OOM, high CPU, thread deadlock) to focused analysis targets
- **Static Code Scan** — Detects 13 anti-patterns across 3 severity levels:
  - **Critical:** N+1 Query, Missing Index/Full Table Scan, Synchronous Blocking, Unbounded Collection Growth, Large Object in Loop
  - **High:** Wide Transaction Scope, Missing Pagination, Inefficient Serialization, Coarse Lock Scope, Thread Pool Misconfiguration
  - **Medium:** Repeated Reflection, Regex in Loop, Auto-boxing in Tight Loop
- **Automated Runtime Diagnosis** — Python script (`java-diag.py`) that collects and analyzes runtime data:
  - **Stack Sampling Mode** — Takes N `jstack` snapshots and identifies hot methods (a method in 80%+ samples = bottleneck)
  - **Full Diagnostic Mode** — Comprehensive health check using JDK tools, Arthas, heap dumps, and external service checks, outputs HTML report
- **Precise Reports** — Findings include exact file paths, line numbers, problematic code snippets, root cause explanation, and concrete fixes — ordered by impact

### Prerequisites

- Claude Code CLI
- Python 3.6+ (for runtime diagnostics, optional dependency: `psutil`)
- JDK tools (`jstack`, `jmap`, `jstat`, `jcmd`, `jinfo`) — for full diagnostic mode
- [Arthas](https://arthas.aliyun.com/) — auto-downloaded in full mode

### Installation

Clone this repository and ensure it's recognized as a Claude Code skill:

```bash
git clone https://github.com/your-username/java-perf-finder.git
cd java-perf-finder
```

The skill is automatically available when this directory is opened in Claude Code.

### Usage

Simply describe your Java performance problem to Claude Code:

```
My Spring Boot API is slow, can you help find the bottleneck?
```

```
The batch job takes too long to finish, please analyze the code.
```

Chinese triggers are also supported:

```
我的接口很慢，帮我看看
```

```
线上服务OOM了，需要定位问题
```

#### Diagnostic Script

For runtime diagnosis, use the included Python script:

```bash
# Stack sampling mode (recommended, most precise)
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --sample

# Full diagnostic mode (comprehensive HTML report)
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --full

# Lite mode (JDK tools only, no Arthas)
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --lite

# With external service checks
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --full --check-redis --check-mysql
```

Key CLI options:

| Option | Description |
|--------|-------------|
| `-p PID` | Target Java process ID |
| `--sample` | Stack sampling mode |
| `--full` | Full diagnostic mode (JDK tools + Arthas) |
| `--lite` | Lite mode (JDK tools only) |
| `--check-redis` | Check Redis connectivity and latency |
| `--check-mysql` | Check MySQL connectivity |
| `--check-pg` | Check PostgreSQL connectivity |
| `--actuator` | Collect Spring Boot Actuator metrics |
| `--thread-filter` | Filter threads by name pattern |
| `--arthas-jar` | Specify Arthas JAR path |
| `--offline` | Offline mode, skip Arthas download |

### Workflow

```
1. Symptom Understanding
   ↓ Classify problem type → map to analysis focus
2. Static Code Scan
   ↓ Grep 13 anti-patterns by severity
2.5. Automated Runtime Diagnosis
   ↓ Stack sampling or full diagnostic
3. Runtime Diagnosis Guide
   ↓ Arthas commands, SQL analysis, memory analysis
4. Comprehensive Report
   → File paths, line numbers, code snippets, root causes, fixes
```

### Project Structure

```
java-perf-finder/
└── .claude/skills/java-perf-finder/
    ├── SKILL.md                    # Skill definition and workflow
    ├── references/
    │   ├── anti-patterns.md        # 13 anti-patterns catalog with grep patterns
    │   └── runtime-tools.md        # Runtime diagnosis tools reference
    └── scripts/
        └── java-diag.py            # Automated diagnostic script
```

### License

MIT

---

<a id="中文"></a>

## Java 性能瓶颈定位器

一个 Claude Code 技能，用于精准定位 Java 项目中的性能瓶颈 —— 慢 SQL、N+1 查询、内存泄漏、线程阻塞、低效循环等。针对 Spring Boot、MyBatis 及通用 Java 应用设计。

### 功能特性

- **症状分类** — 将观察到的症状（API 慢、批处理/任务慢、启动慢、OOM、CPU 高、线程死锁）映射到聚焦的分析目标
- **静态代码扫描** — 检测 3 个严重级别的 13 种反模式：
  - **严重：** N+1 查询、缺失索引/全表扫描、同步阻塞、无界集合增长、循环内大对象
  - **高：** 过大事务范围、缺失分页、低效序列化、粗粒度锁范围、线程池配置不当
  - **中：** 重复反射、循环内正则、紧凑循环内自动装箱
- **自动化运行时诊断** — Python 脚本（`java-diag.py`）采集并分析运行时数据：
  - **栈采样模式** — 采集 N 次 `jstack` 快照并识别热点方法（方法在 80%+ 采样中出现 = 瓶颈）
  - **全量诊断模式** — 使用 JDK 工具、Arthas、堆转储和外部服务检查进行全面健康检查，输出 HTML 报告
- **精准报告** — 输出包含精确文件路径、行号、问题代码片段、根因说明和具体修复方案，按影响排序

### 前置条件

- Claude Code CLI
- Python 3.6+（用于运行时诊断，可选依赖：`psutil`）
- JDK 工具（`jstack`、`jmap`、`jstat`、`jcmd`、`jinfo`）— 全量诊断模式需要
- [Arthas](https://arthas.aliyun.com/) — 全量模式下自动下载

### 安装

克隆本仓库并确保 Claude Code 识别此技能：

```bash
git clone https://github.com/your-username/java-perf-finder.git
cd java-perf-finder
```

在 Claude Code 中打开此目录后，技能自动可用。

### 使用方法

直接向 Claude Code 描述你的 Java 性能问题：

```
My Spring Boot API is slow, can you help find the bottleneck?
```

```
The batch job takes too long to finish, please analyze the code.
```

支持中文触发：

```
我的接口很慢，帮我看看
```

```
线上服务OOM了，需要定位问题
```

#### 诊断脚本

运行时诊断使用内置 Python 脚本：

```bash
# 栈采样模式（推荐，最精准）
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --sample

# 全量诊断模式（完整 HTML 报告）
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --full

# 精简模式（仅 JDK 工具，不使用 Arthas）
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --lite

# 带外部服务检查
python .claude/skills/java-perf-finder/scripts/java-diag.py -p <PID> --full --check-redis --check-mysql
```

主要命令行参数：

| 参数 | 说明 |
|------|------|
| `-p PID` | 目标 Java 进程 ID |
| `--sample` | 栈采样模式 |
| `--full` | 全量诊断模式（JDK 工具 + Arthas） |
| `--lite` | 精简模式（仅 JDK 工具） |
| `--check-redis` | 检查 Redis 连通性和延迟 |
| `--check-mysql` | 检查 MySQL 连通性 |
| `--check-pg` | 检查 PostgreSQL 连通性 |
| `--actuator` | 采集 Spring Boot Actuator 指标 |
| `--thread-filter` | 按名称模式过滤线程 |
| `--arthas-jar` | 指定 Arthas JAR 路径 |
| `--offline` | 离线模式，跳过 Arthas 下载 |

### 工作流程

```
1. 症状理解
   ↓ 分类问题类型 → 映射分析重点
2. 静态代码扫描
   ↓ 按严重级别匹配 13 种反模式
2.5. 自动化运行时诊断
   ↓ 栈采样或全量诊断
3. 运行时诊断指引
   ↓ Arthas 命令、SQL 分析、内存分析
4. 综合报告
   → 文件路径、行号、代码片段、根因、修复方案
```

### 项目结构

```
java-perf-finder/
└── .claude/skills/java-perf-finder/
    ├── SKILL.md                    # 技能定义和工作流程
    ├── references/
    │   ├── anti-patterns.md        # 13 种反模式目录及 grep 匹配模式
    │   └── runtime-tools.md        # 运行时诊断工具参考
    └── scripts/
        └── java-diag.py            # 自动化诊断脚本
```

### 许可证

MIT
