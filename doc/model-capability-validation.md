# 模型能力验证

以下命令在项目根目录的 PowerShell 执行，默认经过 `Higress:8080 -> LiteLLM:4000 -> 模型`。
每次命令都会发起真实调用。`--mode non-stream` 只测一次聊天；去掉它会分别测试非流式、流式。
脚本的聊天 PASS 表示返回了有效文字和协议结束标记，不代表答案正确。

已知接入：公有 DeepSeek、阿里云 API 已由用户验证通过；`my-qwen3.6-27b` 是校内私有部署模型。

本次实测记录：向量接口返回三条 1024 维向量，相关文本相似度 0.681397、无关文本 0.188730。
OCR 图片输入成功，四个顶部统计值均识别正确，但额外输出了下方图表文字，未严格遵守只输出指定区域的要求。
下文推理、编码和图片理解用例尚未在本次执行，需按预期答案继续验收。

## 1. 订阅 / OAuth 账号

订阅是计费方式，OAuth 是授权方式，建议拆成两项记录。例如月套餐可能提供 API Key，
这种情况测试套餐指定的 API 地址、模型、额度和限流即可，不属于 OAuth 验证。
也可能套餐仅供特定客户端使用；是否可经网关调用，以所购套餐的接口说明和使用范围为准。

先明确供应商、订阅产品和它是否提供可供第三方调用的 OAuth API。网页端订阅不等于 API 使用权；
当前配置使用 `api_key`，不能凭此认定 OAuth 已验证。网关自身登录使用 OAuth，也不等于上游模型使用 OAuth。

在供应商和 LiteLLM 对应 provider 支持该方式的前提下，按其文档完成以下测试：

1. 完成一次用户授权，拿到访问令牌及实际授予的权限；记录有效期，不记录令牌正文。
2. 用该授权配置模型，通过网关完成一次真实推理请求，确认使用的是该账号授权。
3. 当访问令牌过期时，验证自动刷新并继续调用；只有供应商支持刷新时才测刷新流程。
4. 在供应商处撤销授权后再次调用，应被拒绝；重新授权后应恢复。
5. 验证订阅额度、限额用尽和权限不足时的错误能返回给客户端。

具体授权地址、scope、回调地址和 provider 配置取决于供应商，不能用一套通用参数代替。
没有此类账号或供应商没有开放相应 API 时，记录“暂不适用”或“当前不支持”，不要填写通过。

## 2. 推理

```powershell
python -X utf8 test/test_gateway_models.py --models my-deepseek-v4-flash --prompt "有四个任务：A耗时2小时；B耗时3小时，必须等A完成；C耗时4小时，必须等A完成；D耗时5小时，必须等B和C都完成。人员无限，任务允许并行。全部完成最短需要几小时？请给出最短时间和关键路径，并简要说明。"
```

人工验收：最短 **11 小时**，关键路径 **A -> C -> D**。B 和 C 可并行，不能简单相加为 14。
可以将模型换成 `my-qwen3.6-27b` 比较结果。单个题目只是基本能力验证，不能证明整体推理水平。

如果还要验证“深度思考模式”的开关、预算或 `reasoning_content` 字段，需要依据该供应商的实际接口说明另测。
当前脚本只检查最终文字及流式协议，不检查或完整输出专用推理字段。回答正确不能证明思考模式开关已生效。

## 3. 编码

```powershell
python -X utf8 test/test_gateway_models.py --models my-kimi-k2.7-code --mode non-stream --prompt "请只输出Python代码，实现merge_intervals(intervals)。输入是闭区间列表，每个元素为[start,end]且start<=end。合并重叠或端点相等的区间，按起点升序输出。空输入返回空列表，不得修改原输入，只使用Python标准库。"
```

人工验收：代码可运行，并通过以下断言；不要仅根据代码看起来合理或 HTTP 200 判定通过。
审阅输出后，在包含该函数的本地测试文件中运行以下内容：

```python
assert merge_intervals([]) == []
assert merge_intervals([[1, 3]]) == [[1, 3]]
assert merge_intervals([[1, 3], [2, 6], [8, 10], [10, 12]]) == [[1, 6], [8, 12]]
assert merge_intervals([[5, 7], [1, 2]]) == [[1, 2], [5, 7]]
assert merge_intervals([[1, 10], [2, 3], [1, 10]]) == [[1, 10]]
assert merge_intervals([[-3, -1], [-1, 0], [2, 2]]) == [[-3, 0], [2, 2]]
original = [[5, 7], [1, 3], [2, 6]]
assert merge_intervals(original) == [[1, 7]]
assert original == [[5, 7], [1, 3], [2, 6]]
```

## 4. 向量

```powershell
python -X utf8 test/test_gateway_models.py --models my-qwen3.7-text-embedding --embedding-models my-qwen3.7-text-embedding
```

脚本调用 `/v1/embeddings`，默认发送三条文本：修改密码、重置密码、天气与户外运动。
协议验收：返回三条向量，index 对齐，无重复，维度相同，数值有限且向量非零。
终端显示每条向量维度、前八个数，以及 input 0 与其余文本的余弦相似度。

语义验收：预期 `Cosine(0,1) > Cosine(0,2)`，即密码相关文本更相似。
脚本不会把这个人工质量检查混同于协议 PASS。若不符合，应换几组领域样本评估，而不是只调整阈值。
模型实际维度以返回值和供应商文档为准，不预设固定维度。

自定义领域输入示例：

```powershell
python -X utf8 test/test_gateway_models.py --models my-qwen3.7-text-embedding --embedding-models my-qwen3.7-text-embedding --embedding-inputs "如何办理校园卡挂失？" "校园卡丢失后怎么挂失？" "图书馆周末几点开门？"
```

## 5. 图片文字识别（OCR）

```powershell
python -X utf8 test/test_gateway_models.py --models my-qwen3.5-ocr --mode non-stream --input-image jpg/3.png --prompt "请按从左到右顺序，逐字识别Overall Usage区域顶部四个统计框的英文标签和对应数值，不要解释。"
```

对照仓库里的原图，预期包含：

| 标签 | 数值 |
| --- | --- |
| Total Requests | 97 |
| Total Successful Requests | 72 |
| Total Tokens | 12,752 |
| Total Spend | $0.00 |

验收文字、数字、标点及对应关系。再用中文通知、表格、低清晰度扫描件测试实际业务场景。
`--input-image` 会把本地图片编码成 data URL，随 `messages[].content` 发到 `/v1/chat/completions`。
前提是该模型端点支持这种图片输入格式；若要求供应商专用 OCR API，应另按其协议接入。

## 6. 图片理解

这项要求视觉模型。不根据别名推断视觉能力；先确认校内部署的 `my-qwen3.6-27b` 是否启用了图片输入。
确认支持后可执行下面命令，否则把模型名换成已接入、已确认支持视觉的聊天模型：

```powershell
python -X utf8 test/test_gateway_models.py --models my-qwen3.6-27b --mode non-stream --input-image jpg/3.png --prompt "根据图中Overall Usage区域，计算总体请求成功率，保留两位小数；再描述右侧Total Requests Over Time图中两条曲线的整体趋势。只依据图片回答。"
```

预期：成功率 `72 / 97 * 100 = 74.23%`；成功请求和失败请求两条曲线整体下降。
这验证跨区域取数、计算和图表理解；OCR 主要验证文字转录，两者不是同一项。
再换一张数值不同的图重复测试，确认模型确实根据图片作答。
图片生成模型 `my-agnes-image-2.5-flash` 不用于这项输入图片的测试。

## 全部模型基本调用

新增向量模型后，原来的全模型测试需要同时声明向量和图片生成模型，否则向量模型仍会走聊天接口：

```powershell
python -X utf8 test/test_gateway_models.py --image-models my-agnes-image-2.5-flash --embedding-models my-qwen3.7-text-embedding
```

这条命令用于基本调用，不替代以上专项测试。建议按“模型、能力、输入样本、预期、实际、通过与否”记录结果。
