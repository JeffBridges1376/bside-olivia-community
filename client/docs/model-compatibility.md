# 模型调用兼容

Olivia 只连接 Olivia 回信服务（`https://175.24.191.6/v1`，模型 `qwen3.7-flash`），用户只需要一个 Olivia 账户 Key。DeepSeek、OpenCode Go、阿里云百炼等自填接口已不再支持，升级后旧的自填配置会被忽略。

任务预算由 `runtime/reply/model_request_policy.py` 管理，与模型参数能力分开。带推理强度的 Qwen 模型在文字信和后台任务中使用 10000 token 上限；语音、视频正文与记忆核对使用 low 档位。截断响应仍拒绝完成或写入。

`runtime/reply/model_capabilities.py` 集中管理推理开关、JSON 模式、流式用量和 tool_choice 差异。Qwen 系列使用 `enable_thinking`；记忆提取明确关闭思考以配合非流式 JSON。未知模型默认发送标准参数，不发送推理扩展。

能力覆盖在启动和保存设置后的记忆重建中传递给 Mem0。适配层在调用 Mem0 SDK 前移除内部配置字段；Key 为空时使用固定占位值，不会让 Mem0 回退到环境里的 `OPENAI_API_KEY`。

部署者可在 `llm_config.json` 的 `provider_options.capabilities` 覆盖代理的实际能力，例如：

```json
{"provider_options":{"capabilities":{"thinking":"none","json_mode":false,"stream_usage":false,"tool_choice":true}}}
```

关闭 JSON wire mode 仅去掉可选参数；业务层仍验证 JSON 内容。鉴权失败、截断输出、空响应和协议错误仍明确报错，不当作成功。能力覆盖不改变服务地址、模型或 Key。

参考：[Qwen OpenAI 兼容思考参数](https://www.alibabacloud.com/help/en/model-studio/batch-inference)。兼容测试使用本地模拟接口，不等同于实机验收。
