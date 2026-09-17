PS D:\zhilin\API网关> python -X utf8 test/test_gateway_models.py --models my-qwen3.6-27b --prompt "A耗时2小时；B耗时3小时，必须等A完成；C耗时4小时，必须等A完成；D耗时5小时，必须等B和C完成。人员无限，任务可并行。全部完成最短需要多久？给出关键路径并简要说明。" " " "              
Route: Higress -> LiteLLM -> model
Gateway: http://localhost:8080/v1
Prompt: A耗时2小时；B耗时3小时，必须等A完成；C耗时4小时，必须等A完成；D耗时5小时，必须等B和C完成。人员无限，任务可并行。全部完成最短需要多久？给出关键路径并简要说明。
Discovered 7 model(s); testing 1 sequentially.

[1/1] my-qwen3.6-27b | non-stream ...
  PASS 21.69s
--- Model output ---


全部完成最短需要 **11小时**。

**关键路径**：`A → C → D`（2 + 4 + 5 = 11小时）

**简要说明**：
1. **A 先行**：耗时 2 小时，第 2 小时末完成。
2. **B、C 并行**：A 完成后 B 和 C 同时开始。B 耗时 3 小时（第 5 小时末结束），C 耗时 4 小时（第 6 小时末结束）。
3. **D 启动条件**：D 必须等 B 和 C 均完成，因此 D 的最早开始时间取两者最晚完成时刻，即第 6 小时末。D 耗时 5 小时，于第 11 小时末全部完工。
4. **关键路径判定**：项目总工期由最长依赖链决定。路径 `A→B→D` 耗时 10 小时，`A→C→D` 耗时 11 小时。故 `A→C→D` 为关键路径。任务 B 有 1 小时浮动时间，提前或延迟 1 小时内均不影响总工期。人员无限且可并行，因此无需考虑资源冲突。
--- End output ---

[1/1] my-qwen3.6-27b | stream ...
--- Streaming output ---


全部完成最短需要 **11小时**。

**关键路径**：`A → C → D`

**简要说明**：
1. **A** 无前置任务，首先执行，耗时 2h（0~2h）。
2. A 完成后，**B** 和 **C** 满足条件可并行启动：
   - B 耗时 3h（2~5h）
   - C 耗时 4h（2~6h）
3. **D** 必须等 B 和 C 均完成才能开始，因此 D 的最早开始时间取决于较晚完成的 C，即第 6h。D 耗时 5h（6~11h）。
4. 项目总工期由网络中的最长路径决定。两条可能路径为：
   - `A→B→D`：2+3+5 = 10h
   - `A→C→D`：2+4+5 = 11h
   最长路径为 `A→C→D`，故最短总工期为 **11小时**。B 虽与 C 并行，但耗时较短，存在 1h 的浮动时间，不构成关键路径。
--- End streaming output ---
  PASS 23.31s | text chunks=265 | first text=18.98s | finish=stop

                      ^C                                                                   
PS D:\zhilin\API网关> python -X utf8 test/test_gateway_models.py --models my-kimi-k2.7-code --mode non-stream --prompt "只输出Python代码，实现merge_intervals(intervals)，合并重叠或端点相等的闭区间，按起点排序。空输入返回空列表，不得修改原输入。"
Route: Higress -> LiteLLM -> model
Gateway: http://localhost:8080/v1
Prompt: 只输出Python代码，实现merge_intervals(intervals)，合并重叠或端点相等的闭区间，按起点排序。空输入返回空列表，不得修改原输入。
Discovered 7 model(s); testing 1 sequentially.

[1/1] my-kimi-k2.7-code | non-stream ...
  PASS 6.00s
--- Model output ---
```python
def merge_intervals(intervals):
    if not intervals:
        return []

    # 不修改原输入，对副本排序
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [list(sorted_intervals[0])]

    for current in sorted_intervals[1:]:
        last = merged[-1]
        if current[0] <= last[1]:  # 重叠或端点相接
            if current[1] > last[1]:
                last[1] = current[1]
        else:
            merged.append(list(current))

    return merged
```
--- End output ---


Total: 1 | Passed: 1 | Failed: 0
PS D:\zhilin\API网关> python -X utf8 test/test_gateway_models.py --models my-qwen3.7-text-embedding --embedding-models my-qwen3.7-text-embedding
Route: Higress -> LiteLLM -> model
Gateway: http://localhost:8080/v1
Prompt: 请用中文简要介绍你自己，三句话以内。
Embedding input 0: 如何修改账户密码？
Embedding input 1: 忘记密码后怎样重置登录密码？
Embedding input 2: 今天的天气晴朗，适合户外运动。
Discovered 7 model(s); testing 1 sequentially.

[1/1] my-qwen3.7-text-embedding | embedding ...
Vector 0: dimensions=1024 | first 8=[0.01864243857562542, 0.05200711637735367, 0.02371780201792717, 0.03981641307473183, 0.025929825380444527, 0.02074385993182659, 0.047927163541316986, -0.10706191509962082]
Vector 1: dimensions=1024 | first 8=[0.016907531768083572, 0.045226722955703735, 0.03085099719464779, 0.0331481508910656, 0.055181048810482025, 0.0013430928811430931, 0.036976736038923264, -0.11174532771110535]
Vector 2: dimensions=1024 | first 8=[0.030395884066820145, 0.01968437433242798, -0.108744315803051, -0.045253392308950424, 0.00740443728864193, 0.03105243481695652, -0.026578161865472794, -0.04075480252504349]
Cosine(input 0, input 1) = 0.681397
Cosine(input 0, input 2) = 0.188730
  PASS 0.28s | vectors=3 (protocol check)


Total: 1 | Passed: 1 | Failed: 0
PS D:\zhilin\API网关> python -X utf8 test/test_gateway_models.py --models my-qwen3.5-ocr --mode non-stream --input-image jpg/3.png --prompt "识别Overall Usage顶部四个统计框的英文标签和数值。"
Route: Higress -> LiteLLM -> model
Gateway: http://localhost:8080/v1
Prompt: 识别Overall Usage顶部四个统计框的英文标签和数值。
Input image: D:\zhilin\API网关\jpg\3.png
Discovered 7 model(s); testing 1 sequentially.

[1/1] my-qwen3.5-ocr | non-stream ...
  PASS 2.84s
--- Model output ---
```json
{
    "label": "Overall Usage",
    "value": {
        "Total Requests": "97",
        "Total Successful Requests": "72",
        "Total Tokens": "12,752",
        "Total Spend": "$0.00"
    }
}
```
--- End output ---
                      python -X utf8 test/test_gateway_models.py --models my-qwen3.6-27b --mode non-stream --input-image jpg/3.png --prompt "根据Overall Usage计算总体请求成功率，保留两位小数；描述右侧Total Requests Over Time两条曲线的整体趋势。"
Route: Higress -> LiteLLM -> model
Gateway: http://localhost:8080/v1
Prompt: 根据Overall Usage计算总体请求成功率，保留两位小数；描述右侧Total Requests Over Time两条曲线的整体趋势。
Input image: D:\zhilin\API网关\jpg\3.png
Discovered 7 model(s); testing 1 sequentially.

[1/1] my-qwen3.6-27b | non-stream ...
  PASS 13.39s
--- Model output ---


**总体请求成功率计算：**

根据 "Overall Usage" 板块的数据：
*   Total Requests（总请求数）：97
*   Total Successful Requests（总成功请求数）：72

成功率 = (72 ÷ 97) × 100% ≈ **74.23%**

***

**右侧 Total Requests Over Time 曲线趋势描述：**

该图表展示了随时间变化的请求数量，包含两条曲线：
1.  **整体呈显著下降趋势**：无论是成功请求（绿色线）还是失败请求（红色线），在时间轴初期（左侧）数值较高（分别约为60多和80多），随后迅速下降，在时间轴后期（右侧）均降至非常低的水平（接近个位数）。
2.  **失败请求始终高于成功请求**：代表失败请求的红色曲线（metrics.failed_requests）在整个时间段内始终位于代表成功请求的绿色曲线（metrics.successful_requests）上方，说明在该统计周期内，失败的请求数量一直多于成功的请求数量。
--- End output ---


Total: 1 | Passed: 1 | Failed: 0


'''
用户名：admin
密码：sk-local-test
'''
sk-_vg3-n04uVYtbqqK38AElg