# Java Performance Anti-Patterns

## Table of Contents
- [1. N+1 Query](#1-n1-query)
- [2. Missing Index / Full Table Scan](#2-missing-index--full-table-scan)
- [3. Synchronous Blocking](#3-synchronous-blocking)
- [4. Unbounded Collection Growth](#4-unbounded-collection-growth)
- [5. Large Object in Loop](#5-large-object-in-loop)
- [6. Wide Transaction Scope](#6-wide-transaction-scope)
- [7. Missing Pagination](#7-missing-pagination)
- [8. Inefficient Serialization](#8-inefficient-serialization)
- [9. Coarse Lock Scope](#9-coarse-lock-scope)
- [10. Thread Pool Misconfiguration](#10-thread-pool-misconfiguration)
- [11. Repeated Reflection](#11-repeated-reflection)
- [12. Regex in Loop](#12-regex-in-loop)
- [13. Auto-boxing in Tight Loop](#13-auto-boxing-in-tight-loop)

---

## 1. N+1 Query

**Grep patterns:**
```
mapper\.select|mapper\.find|mapper\.query|mapper\.get
```
Then check if inside `for` / `while` / `forEach` / `stream().map` body.

**Bad:**
```java
for (Order order : orders) {
    List<Item> items = itemMapper.selectByOrderId(order.getId());
    order.setItems(items);
}
```

**Good:**
```java
List<Long> orderIds = orders.stream().map(Order::getId).collect(Collectors.toList());
Map<Long, List<Item>> itemMap = itemMapper.selectByOrderIds(orderIds)
    .stream().collect(Collectors.groupingBy(Item::getOrderId));
orders.forEach(o -> o.setItems(itemMap.getOrDefault(o.getId(), Collections.emptyList())));
```

**Impact:** N records → N+1 DB round trips. For 1000 records, that's 1001 queries vs 1.

---

## 2. Missing Index / Full Table Scan

**Grep patterns:**
```
LIKE '%|WHERE\s+\w+\s+!=|WHERE\s+\w+\s+IS NOT|fn_\w+\(|YEAR\(|DATE_FORMAT\(
```

**Common causes:**
- `LIKE '%keyword'` — leading wildcard cannot use index
- Function on indexed column: `WHERE YEAR(create_time) = 2024` → should be `WHERE create_time >= '2024-01-01' AND create_time < '2025-01-01'`
- `OR` conditions that break index merge
- Implicit type conversion: `WHERE varchar_col = 123` (number vs string)

**MyBatis XML check:**
- Look at `<select>` statements, examine `WHERE` and `ORDER BY` clauses
- Cross-reference with `CREATE INDEX` or `@Index` annotations in entity classes

---

## 3. Synchronous Blocking

**Grep patterns:**
```
RestTemplate|OkHttpClient|HttpClient\.new|feign\.\w+Client|Thread\.sleep
```

**Bad:**
```java
public OrderVO getOrder(Long id) {
    Order order = orderMapper.selectById(id);    // DB call
    User user = userClient.getUser(order.getUserId()); // RPC call, blocks thread
    Product product = productClient.getProduct(order.getProductId()); // another RPC
    return assemble(order, user, product);
}
```

**Good:**
```java
// Parallel fetch with CompletableFuture
CompletableFuture<User> userFuture = CompletableFuture.supplyAsync(
    () -> userClient.getUser(order.getUserId()), executor);
CompletableFuture<Product> productFuture = CompletableFuture.supplyAsync(
    () -> productClient.getProduct(order.getProductId()), executor);
User user = userFuture.join();
Product product = productFuture.join();
```

---

## 4. Unbounded Collection Growth

**Grep patterns:**
```
static\s+(Map|List|Set|HashMap|ArrayList|HashSet|ConcurrentHashMap|ConcurrentMap)
```
Then check: is there any removal/eviction logic?

**Bad:**
```java
private static final Map<String, CacheData> CACHE = new ConcurrentHashMap<>();
// Never removed → memory leak
```

**Good:**
```java
private static final Cache<String, CacheData> CACHE = Caffeine.newBuilder()
    .maximumSize(10_000)
    .expireAfterWrite(Duration.ofMinutes(30))
    .build();
```

**Also check:** `ThreadLocal` without `remove()` in servlet/async context.

---

## 5. Large Object in Loop

**Grep patterns:**
```
new byte\[|new String\[|StringBuilder|String\s*\+\s*\w+\s*;(?=.*for)
```

**Bad:**
```java
String result = "";
for (Item item : items) {
    result += item.toString(); // Creates new String each iteration
}
```

**Good:**
```java
StringBuilder sb = new StringBuilder(items.size() * 64);
for (Item item : items) {
    sb.append(item.toString());
}
String result = sb.toString();
```

---

## 6. Wide Transaction Scope

**Grep patterns:**
```
@Transactional
```
Then read the annotated method body.

**Bad:**
```java
@Transactional
public void createOrder(OrderDTO dto) {
    validateOrder(dto);           // pure logic, no DB
    orderMapper.insert(order);    // DB write
    notifyService.send(msg);      // RPC call, slow
    logService.record(log);       // another DB, maybe different DS
}
```
DB connection is held during RPC call → connection pool exhaustion under load.

**Good:**
```java
@Transactional
public void insertOrder(Order order) {
    orderMapper.insert(order);
}

public void createOrder(OrderDTO dto) {
    validateOrder(dto);
    insertOrder(order);           // transaction only around DB write
    notifyService.send(msg);      // outside transaction
}
```

---

## 7. Missing Pagination

**Grep patterns:**
```
selectAll|findAll|listAll|findBy\w+Without\s|selectList\s*\(.*\)\s*;(?![\s\S]*PageHelper|PageRequest|LIMIT|page)
```

**Bad:**
```java
List<Order> orders = orderMapper.selectAll(); // Loads entire table
```

**Good:**
```java
PageHelper.startPage(pageNum, pageSize);
List<Order> orders = orderMapper.selectAll();
```

---

## 8. Inefficient Serialization

**Grep patterns:**
```
new ObjectMapper\(\)|JSON\.parse|JSON\.toJSON|JSONObject\.|Gson\s+gson\s*=\s*new\s+Gson
```
Check if called inside loop or method called frequently.

**Bad:**
```java
for (Event event : events) {
    ObjectMapper mapper = new ObjectMapper(); // heavy object, don't recreate
    String json = mapper.writeValueAsString(event);
}
```

**Good:**
```java
private static final ObjectMapper MAPPER = new ObjectMapper();
// reuse MAPPER everywhere
```

---

## 9. Coarse Lock Scope

**Grep patterns:**
```
synchronized\s*\(|synchronized\s+void|ReentrantLock\s*\(.*\)\.lock\(\)
```

**Bad:**
```java
public synchronized void process(Order order) {
    validate(order);        // no shared state, doesn't need lock
    update(order);          // needs lock
    notify(order);          // no shared state
}
```

**Good:**
```java
public void process(Order order) {
    validate(order);
    synchronized (this) {
        update(order);
    }
    notify(order);
}
```

---

## 10. Thread Pool Misconfiguration

**Grep patterns:**
```
ThreadPoolExecutor|Executors\.|newFixedThreadPool|newCachedThreadPool|@Async
```

**Common issues:**
- `Executors.newCachedThreadPool()` — unbounded thread count → OOM
- `Executors.newFixedThreadPool(n)` with `LinkedBlockingQueue()` — unbounded queue → tasks never rejected but never execute
- `@Async` without custom executor → uses SimpleAsyncTaskExecutor (creates new thread per task)

**Good configuration:**
```java
new ThreadPoolExecutor(
    8,                              // corePoolSize
    32,                             // maxPoolSize
    60, TimeUnit.SECONDS,           // keepAlive
    new LinkedBlockingQueue<>(1000), // bounded queue
    new ThreadPoolExecutor.CallerRunsPolicy() // back-pressure
);
```

---

## 11. Repeated Reflection

**Grep patterns:**
```
Class\.forName|Method\.invoke|Field\.setAccessible|getDeclaredMethod
```

**Fix:** Cache `Method`/`Field` objects after first lookup. Use `MethodHandle` or generated bytecode (like cglib) for repeated access.

---

## 12. Regex in Loop

**Grep patterns:**
```
Pattern\.compile\(
```
Check if called inside a method that runs in a loop or is frequently invoked.

**Bad:**
```java
public boolean isValid(String input) {
    return Pattern.compile("^[A-Z]\\d{6}$").matcher(input).matches();
}
```

**Good:**
```java
private static final Pattern VALID_PATTERN = Pattern.compile("^[A-Z]\\d{6}$");
public boolean isValid(String input) {
    return VALID_PATTERN.matcher(input).matches();
}
```

---

## 13. Auto-boxing in Tight Loop

**Grep patterns:**
```
Integer\s+\w+\s*=|Long\s+\w+\s*=|Double\s+\w+\s*=
```
Inside high-frequency loops.

**Bad:**
```java
Long sum = 0L;
for (int i = 0; i < 1_000_000; i++) {
    sum += i; // Creates ~1M Long objects via auto-boxing
}
```

**Good:**
```java
long sum = 0L;
for (int i = 0; i < 1_000_000; i++) {
    sum += i; // primitive, no object allocation
}
```
