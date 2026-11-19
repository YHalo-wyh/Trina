# 反编译策略指南

## 目录

- [何时反编译](#何时反编译)
- [反编译工具使用](#反编译工具使用)
- [反编译结果分析](#反编译结果分析)
- [常见问题](#常见问题)

---

## 何时反编译

### 必须反编译的场景

1. **接口定义在 .class 文件中**
   - 项目只包含编译后的字节码
   - 依赖的库仅提供 JAR 包

2. **参数类型需要深入提取**
   - DTO/POJO 类无源码
   - 复杂的泛型类型需要确定具体类型

3. **第三方框架扩展**
   - 自定义的注解处理器
   - 框架的内部实现

### 不需要反编译的场景

1. 源码已存在且可读取
2. 标准库/JDK 类
3. 已有文档的第三方库

---

## 反编译工具使用

### MCP Java Decompiler

#### 单个文件反编译

```python
# 反编译单个 .class 文件
mcp__java-decompile-mcp__decompile_file(
    file_path="/path/to/MyController.class",
    output_dir="/path/to/output"  # 可选，默认为 ./decompiled
)
```

#### 目录反编译

```python
# 递归反编译整个目录
mcp__java-decompile-mcp__decompile_directory(
    directory_path="/path/to/classes",
    output_dir="/path/to/output",
    recursive=True
)
```

#### 批量文件反编译

```python
# 反编译多个文件
mcp__java-decompile-mcp__decompile_files(
    file_paths=[
        "/path/to/UserController.class",
        "/path/to/ProductController.class",
        "/path/to/OrderController.class"
    ],
    output_dir="/path/to/output"
)
```

### 反编译策略

#### 策略 1: 最小化反编译

```python
# 只反编译需要的类，而非整个项目
target_classes = []

# 1. 从源码中找到需要反编译的类引用
for controller in source_controllers:
    for param_type in controller.parameter_types:
        if param_type not in source_files:
            target_classes.append(param_type)

# 2. 只反编译这些类
for class_file in target_classes:
    decompile_file(class_file)
```

#### 策略 2: 层级反编译

```python
# 1. 先反编译控制器类
decompile_files(controller_classes)

# 2. 识别需要深入反编译的类型
required_types = extract_types_from_controllers()

# 3. 反编译参数类型
decompile_files(required_types)

# 4. 如果参数类型是嵌套对象，继续反编译
nested_types = extract_nested_types(required_types)
decompile_files(nested_types)
```

#### 策略 3: 增量缓存

```python
# 维护反编译缓存
decompiled_cache = {}

def get_decompiled(class_file):
    if class_file not in decompiled_cache:
        decompiled_cache[class_file] = decompile_file(class_file)
    return decompiled_cache[class_file]
```

---

## 反编译结果分析

### 识别关键信息

```java
// 反编译后的控制器示例
@RestController
@RequestMapping("/api/users")
public class UserController {

    @GetMapping("/{id}")
    public User getUser(@PathVariable("id") Long id) {
        // 方法实现
    }

    @PostMapping
    public User create(@RequestBody UserDto dto) {
        // 方法实现
    }
}
```

**提取内容：**
- 类名: `UserController`
- 基础路径: `/api/users`
- 方法 1: `getUser` → GET `/api/users/{id}`, 参数 `id: Long`
- 方法 2: `create` → POST `/api/users`, 参数 `dto: UserDto`

### 参数类型分析

```java
// 反编译后的 DTO
public class UserDto {
    private String username;
    private String password;
    private Integer age;
    private List<String> roles;
    private ProfileDto profile;

    // getter/setter
}
```

**提取内容：**
- `username`: String
- `password`: String
- `age`: Integer
- `roles`: List<String>
- `profile`: ProfileDto (需要进一步分析)

### 泛型类型解析

```java
// 原始代码
public Response<List<User>> listUsers() { }

// 反编译后可能丢失泛型
public Response listUsers() { }
```

**处理策略：**
1. 检查方法返回值的使用
2. 分析方法体内的类型转换
3. 参考相关测试代码

### 匿名内部类

```java
// 反编译可能显示为
new Comparator() {
    public int compare(Object o1, Object o2) {
        // ...
    }
}
```

**处理策略：**
- 匿名类通常不影响路由结构
- 可忽略或标注为内部实现

---

## 常见问题

### 问题 1: 反编译失败

**可能原因：**
- 文件损坏
- 不支持的 Java 版本
- 混淆的代码

**解决方案：**
```python
# 检查 Java 版本
mcp__java-decompile-mcp__get_java_version()

# 尝试使用不同的反编译器
# 或记录为"无法反编译"
```

### 问题 2: 反编译结果不完整

**表现：**
- 缺少方法体
- 变量名被混淆

**影响：**
- 路由结构仍然可识别（注解保留）
- 参数名可能丢失，但类型可推断

**解决方案：**
```python
# 从注解中提取信息
annotations = extract_annotations(method)

# 从参数类型中提取信息
param_types = extract_parameter_types(method)

# 即使变量名丢失，仍然可以生成模板
generate_template(annotations, param_types)
```

### 问题 3: Lambda 表达式

```java
// 原始代码
users.stream().filter(u -> u.getAge() > 18).collect(toList());

// 反编译后可能显示为
users.stream().filter(new Predicate() {
    public boolean test(Object u) {
        return ((User)u).getAge() > 18;
    }
}).collect(toList());
```

**处理策略：**
- Lambda 不影响路由分析
- 关注注解和方法签名

### 问题 4: 枚举类型

```java
// 反编译结果
public enum UserRole {
    ADMIN, USER, GUEST;
}
```

**提取内容：**
- 枚举类: `UserRole`
- 可能值: `ADMIN`, `USER`, `GUEST`

**请求模板：**
```
# 枚举参数的可能值
role=ADMIN
role=USER
role=GUEST
```

---

## 反编译结果验证

### 验证清单

- [ ] 类路径与预期一致
- [ ] 注解信息完整
- [ ] 方法签名清晰
- [ ] 参数类型可解析
- [ ] 泛型信息合理

### 对比源码（如果可用）

```python
def verify_decompiled(source_file, decompiled_content):
    # 对比类名
    assert extract_class_name(source) == extract_class_name(decompiled)

    # 对比方法签名
    source_methods = extract_method_signatures(source)
    decompiled_methods = extract_method_signatures(decompiled)
    assert set(source_methods) == set(decompiled_methods)

    # 对比注解
    assert compare_annotations(source, decompiled)
```

---

## 记录反编译来源

```python
# 输出时标注反编译来源
{
    "route": "/api/users/{id}",
    "method": "GET",
    "parameters": [
        {
            "name": "id",
            "type": "Long",
            "source": "decompiled: UserController.class:45"
        }
    ],
    "controller_location": "decompiled: UserController.class"
}
```

---

## 性能优化

### 批量操作

```python
# 一次性反编译多个文件，减少启动开销
decompile_files(list_of_class_files)
```

### 并行处理

```python
# 对于独立文件，可并行反编译
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [executor.submit(decompile_file, f) for f in class_files]
    results = [f.result() for f in futures]
```

### 缓存策略

```python
# 缓存反编译结果
import hashlib
import os

def get_decompiled_cache_path(class_file):
    hash_key = hashlib.md5(class_file.encode()).hexdigest()
    return f"cache/decompiled/{hash_key}.java"

def decompile_with_cache(class_file):
    cache_path = get_decompiled_cache_path(class_file)
    if os.path.exists(cache_path):
        return read_file(cache_path)
    result = decompile_file(class_file)
    write_file(cache_path, result)
    return result
```

---

## 反编译与源码混合

当项目同时包含源码和编译文件时：

```python
def get_class_info(class_name):
    # 优先使用源码
    source_file = find_source_file(class_name)
    if source_file and is_readable(source_file):
        return parse_source_file(source_file)

    # 源码不存在则反编译
    class_file = find_class_file(class_name)
    if class_file:
        return decompile_and_parse(class_file)

    # 都不存在
    return None
```
