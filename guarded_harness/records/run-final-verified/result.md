# 登录会话模块：测试补充与缺陷修复

## 结论
`run_test[default]` **PASS**（exit_code=0，7/7 通过）。

## 问题根因
`src/auth/session-policy.ts` 中的 `isSessionExpired` 只比较"日期部分"：

```ts
const expiresOn = session.expiresAt.slice(0, 10);
const currentDay = now.toISOString().slice(0, 10);
return expiresOn < currentDay;
```

这会导致同一天内的过期判断全部失效，边界用例因此失败。

## 修复
改为按时间戳精确比较，过期时刻及之后即视为过期：

```ts
export interface ExpiringSession {
  expiresAt: string;
}

export function isSessionExpired(session: ExpiringSession, now: Date): boolean {
  return now.getTime() >= new Date(session.expiresAt).getTime();
}
```

## 测试覆盖（新增/现有）
| 用例 | 期望 | 结果 |
| --- | --- | --- |
| 到期前仍然有效（00:29:59.999） | false | ✔ |
| 到期时刻视为已过期（00:30:00.000） | true | ✔ |
| 到期后立即失效（00:30:00.001） | true | ✔ |
| 会话在过期时刻之后（session-policy.test.ts） | true | ✔ |
| 会话在过期时刻之前（session-policy.test.ts） | false | ✔ |
| 登录成功后返回会话令牌 | — | ✔ |
| 密码错误时登录失败 | — | ✔ |

## 测试输出摘要
```text
tests 7
pass 7
fail 0
```
