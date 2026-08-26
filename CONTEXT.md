# Mini LLM Gateway

为业务 Agent 统一代理 LLM 调用的网关：收敛模型访问、Prompt 资产与调用审计，业务方不接触供应商密钥。

## Language

### 模型

**平台模型**:
Gateway 对外发布的模型别名（如 general-primary），调用方只用它；映射到供应商模型、地址与价格。
_Avoid_: 模型名、alias

**供应商模型**:
上游供应商真实接受的模型标识（如 deepseek-chat），只在供应商适配层出现。
_Avoid_: provider_model 之外的"真实模型"等说法

**备用模型（Fallback）**:
主模型经有限重试仍不可用时，接管的另一个平台模型；必须在白名单内且能力等价。
_Avoid_: 降级模型

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
