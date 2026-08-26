# Mini LLM Gateway

为业务 Agent 统一代理 LLM 调用的网关：收敛模型访问、Prompt 资产与调用审计，业务方不接触供应商密钥。

## Language

### 模型与路由

**档位（Mode）**:
调用方表达的意图档位（如 fast、smart），是 model 字段的唯一合法取值；网关据其在候选池中路由，调用方不能指定具体模型。
_Avoid_: 模型别名、级别、平台模型

**路由目标（Route Target）**:
档位候选池中的一项，由供应商、模型、权重、能力与价格构成；选择与健康度都以它为粒度，标识格式为 `供应商/模型`。
_Avoid_: 节点、后端、实例

**供应商模型**:
上游供应商真实接受的模型标识（如 deepseek-chat），只作为路由目标的组成部分出现。
_Avoid_: provider_model 之外的"真实模型"等说法

**requested_model（请求模型）**:
调用方在请求里传入的档位名；响应的 `model` 字段回显它。
_Avoid_: 请求别名、model

**actual_model（实际模型）**:
单次调用中网关实际选中的路由目标（`供应商/模型`）；fallback 后与 requested_model 必然不同，以扩展字段返回并记入 Trace。
_Avoid_: resolved_model、真实模型

**Fallback（候选切换）**:
主选目标失败后按剩余健康候选依次接管的机制；流式调用仅首块前可切换。
_Avoid_: 降级模型

**熔断（Circuit Breaker）**:
按路由目标维护的可用性状态：连续失败超阈值即熔断，冷却期后半开试探一次；全档位目标熔断时该档位不可用。
_Avoid_: 拉黑、摘流

### 模板

**Prompt 模板**:
由 Gateway 发布和版本化管理的系统提示词资产，用 Jinja2 书写，正文只能由管理方维护，调用方只能选择 (name, version) 和传变量。
_Avoid_: 提示词、prompt

**版本**:
模板的不可变发布单位，(name, version) 唯一；"修改"即创建新版本，历史版本永久保留。
_Avoid_: 修订、快照

**渲染**:
用调用方变量填充模板得到系统消息的过程；缺变量视为错误而非静默忽略。
_Avoid_: 填充、填充模板

### 协议

**兼容端点**:
说 OpenAI 官方方言的对外入口，共两个：/v1/chat/completions（Chat Completions 方言）与 /v1/responses（Responses 方言）；已有 OpenAI SDK 可无缝接入。
_Avoid_: OpenAI 端点、chat 端点

**请求规范化**:
把外部兼容请求翻译成内部领域模型的过程，只发生在 API 层边界；内部各层不认识任何 OpenAI 方言。
_Avoid_: 转换、适配

**gateway_prompt**:
兼容协议里的非标扩展字段，承载模板选择 (name, version, variables)，SDK 经 extra_body 传入。
_Avoid_: prompt 参数、模板字段

**会话续接**:
Responses 端点的多轮对话机制：store=true 的成功响应存入服务端，调用方以 previous_response_id 引用其上下文继续对话；存储的是展开后的完整输入而非链式指针。
_Avoid_: 上下文缓存、历史记录

### 调用与审计

**Trace（调用追踪）**:
单次 LLM 调用的元数据记录（模型、Token、成本、延迟、尝试次数、状态），不含对话文本；只追加、不修改。
_Avoid_: 日志、usage

**结构化输出**:
调用方声明 JSON Schema，Gateway 保证返回内容通过该 Schema 校验的输出方式；与流式互斥。
_Avoid_: JSON 模式

**管理令牌**:
模板写操作所需的静态凭证，从环境变量注入；与调用方身份无关。
_Avoid_: API Key（那是供应商凭据）
