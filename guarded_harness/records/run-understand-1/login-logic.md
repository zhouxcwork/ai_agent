# 登录逻辑说明（Login Logic）

## 1. 概览

项目采用「路由 → 服务 → 仓储 → 数据源」的分层结构，登录主链路如下：

```
POST /api/login
  └─ loginRouter            (src/routes/login.ts)
       └─ authService.login (src/services/auth-service.ts)
            ├─ userRepository.findByEmail   (src/repositories/user-repository.ts)
            │    └─ queryOne("users", { email })  (src/db/database.ts)
            ├─ verifyPassword               (src/utils/password.ts)
            └─ issueAccessToken             (src/utils/token.ts)
```

登录成功签发的 accessToken 由 `authMiddleware`（`src/middleware/auth-middleware.ts`）在后续受保护接口中校验。

## 2. 关键文件

| 文件 | 职责 |
| --- | --- |
| `src/routes/login.ts` | 登录入口 `POST /api/login`，参数校验与状态码映射 |
| `src/services/auth-service.ts` | 登录业务编排：查用户、验密码、签发 token |
| `src/repositories/user-repository.ts` | 按 email 查询 `users` 表 |
| `src/db/database.ts` | 模拟数据库，内存中的 `users` 表 |
| `src/middleware/auth-middleware.ts` | Bearer Token 认证中间件 |
| `src/utils/password.ts` | 密码校验（demo 实现） |
| `src/utils/token.ts` | token 签发与解析 |
| `src/types/user.ts` | `UserRecord` / `LoginResult` / `AuthTokenPayload` 类型定义 |

## 3. 详细流程

### 3.1 路由层：`POST /api/login`

1. 从 `request.body` 解构 `email`、`password`。
2. 任一缺失 → `400 { error: "email and password are required" }`。
3. 调用 `authService.login(email, password)`。
4. 成功 → `200` 返回结果；捕获异常 → `401 { error: message }`（异常信息原样透出，如 "User not found" / "Invalid password"）。

### 3.2 服务层：`AuthService.login`

1. `userRepository.findByEmail(email)` 查询用户。
2. 用户不存在 → `throw new Error("User not found")`。
3. `verifyPassword(password, user.passwordHash)`：
   - 不匹配 → `throw new Error("Invalid password")`。
4. 匹配成功 → `issueAccessToken({ userId, role })`。
5. 返回 `LoginResult`：

```ts
{
  accessToken,
  user: { id, email, displayName, role }   // 不含 passwordHash
}
```

### 3.3 仓储与数据源

- `UserRepository.findByEmail` → `queryOne("users", { email })`。
- `database.ts` 中 `users` 为内存数组，示例数据：
  - `demo@example.com` / `hash:demo123` / member
  - `admin@example.com` / `hash:admin123` / admin

### 3.4 密码校验：`verifyPassword`

```ts
return passwordHash === `hash:${plainTextPassword}`;
```

即存储值等于 `"hash:" + 明文密码`，仅为**演示用的假哈希**。

### 3.5 Token 签发与解析

- 签发：`"demo-token." + base64url(JSON.stringify(payload))`，payload 为 `{ userId, role }`。
- 解析：校验前缀 `demo-token.`，base64url 解码后 `JSON.parse`，失败返回 `null`。

### 3.6 认证中间件：`authMiddleware`

1. 读取 `Authorization` 头，必须形如 `Bearer <token>`，否则 `401 { error: "Missing bearer token" }`。
2. `parseAccessToken(token)` 返回 `null` → `401 { error: "Invalid token" }`。
3. 成功则将 payload 挂到 `response.locals.currentUser`，调用 `next()`。

## 4. 时序图

```
Client           Router          AuthService     UserRepository     DB
  | POST /api/login  |                 |                |            |
  |----------------->| login(email,pw) |                |            |
  |                  |---------------->| findByEmail    |            |
  |                  |                 |--------------->| queryOne   |
  |                  |                 |                |----------->|
  |                  |                 |<---------------| user/undefined
  |                  |                 | verifyPassword |            |
  |                  |                 | issueAccessToken            |
  |<-----------------| 200 { accessToken, user }                     |
```

失败路径：用户不存在 / 密码错误 → 服务层 throw → 路由层捕获 → `401`。

## 5. 错误码约定

| 场景 | 状态码 | 响应体 |
| --- | --- | --- |
| 缺少 email 或 password | 400 | `{ "error": "email and password are required" }` |
| 用户不存在 | 401 | `{ "error": "User not found" }` |
| 密码错误 | 401 | `{ "error": "Invalid password" }` |
| 无 Bearer Token | 401 | `{ "error": "Missing bearer token" }` |
| Token 非法 | 401 | `{ "error": "Invalid token" }` |

## 6. 安全风险与改进建议

1. **密码等同于明文存储**：`hash:` 前缀方案无实际哈希，应改用 bcrypt/argon2 加盐哈希 + 恒定时间比较。
2. **token 未签名**：base64url 只是编码，可被任意伪造（可自造 admin payload）；生产环境应使用 JWT + HMAC/RSA 签名，设置并校验 `exp`/`iat`/`iss`。
3. **错误信息泄露**：区分 "User not found" 与 "Invalid password" 便于账号枚举，建议统一为 "Invalid credentials"。
4. **缺少防暴力破解**：无速率限制 / 账号锁定，建议增加限流与连续失败锁定。
5. **未校验 email 格式**：建议引入 schema 校验（如 zod）。
6. **无 refresh token / 登出机制**：建议引入 refresh token 与 token 黑名单。
7. **缺少日志与审计**：登录成功/失败应记录脱敏日志。
8. **中间件缺少授权**：仅有身份校验，建议补充 `requireRole("admin")` 等授权中间件。
