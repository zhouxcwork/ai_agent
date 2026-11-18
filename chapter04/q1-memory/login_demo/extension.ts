import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { executeMemory } from "./memory-store.js";

const NonEmpty = Type.String({ minLength: 1 });
const MemoryRecord = Type.Object({
  testPath: Type.String({ minLength: 1, description: "相对项目根目录的测试路径" }),
  command: Type.String({ minLength: 1, description: "在项目根目录已实跑的测试命令" }),
  verification: Type.Optional(Type.String({ description: "本次实际观察到的测试结果。未运行时省略。" })),
  coverage: Type.Optional(Type.Array(NonEmpty, { description: "从测试文件核实的覆盖场景。" })),
  sources: Type.Array(NonEmpty, { minItems: 1, description: "已核实的来源文件，使用项目相对路径" }),
}, { additionalProperties: false });

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "project_memory",
    label: "项目长期记忆",
    description: "从 pi 启动目录读写本项目 Markdown 记忆。会话测试入口使用 session-test-entry。read 可省略 ID 查找当前项目的记忆。write 和 update 仅登记已核实的项目相对路径、命令、来源和实际测试结果。工具不运行命令。",
    parameters: Type.Object({
      action: Type.Union([
        Type.Literal("write"), Type.Literal("read"),
        Type.Literal("update"), Type.Literal("delete"),
      ]),
      id: Type.Optional(NonEmpty),
      record: Type.Optional(MemoryRecord),
    }, { additionalProperties: false }),
    execute(_toolCallId, request, _signal, _onUpdate, ctx) {
      return executeMemory(ctx.cwd, request);
    },
  });
}
