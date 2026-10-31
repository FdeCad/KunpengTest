# 上游问题分析与处理机制

> 适用对象：`openeuler/openeuler-docker-images` 在鲲鹏（Kunpeng 920 / 950）上的容器镜像 CI 测试与报告链路。
> 对应实现：`update/container/app/update.py`（check / push 两个阶段）、`schema/test-result.schema.json`（报告契约，当前 `0.5`）。

## 1. 目的

把“镜像在鲲鹏上跑不起来”拆解成可判定、可记录、可追溯的结论，避免两种极端：

- 把所有失败都归因给上游，导致上游收到无效 issue；
- 把所有失败都留给自己，导致问题长期无人跟进。

本机制规定：**问题如何分类、结论写进哪个字段、什么情况下阻塞 PR、什么情况下需要人工复核、什么时候需要找上游。**

## 2. 问题分类

| 类别 | 典型现象 | 归属 | 允许结论为“上游不支持” |
| --- | --- | --- | --- |
| 上游不支持 | 上游没有 aarch64 产物；上游文档/issue 明确声明不支持 | 上游 | 是 |
| 上游缺陷 | 未做本地修改，上游源码在 aarch64 上编译失败或运行崩溃 | 上游 | 是，需附证据 |
| 镜像构建问题 | Dockerfile 写错、基础镜像不存在、依赖安装失败、路径错误 | 本仓库 | 否 |
| 测试脚本问题 | `test.sh` 与服务启动方式不匹配、输出格式不符合解析约定、用例函数顺序与输出不一致 | 本仓库 | 否 |
| 基础设施问题 | 节点资源不足、网络超时、registry 不可用、docker daemon 异常 | CI 环境 | 否 |

判定原则：**只有排除“本仓库 / 测试脚本 / CI 环境”三类原因后，才允许在报告中写 `upstream_supported=false` 并把问题升级到上游。**

## 3. 结论字段与判定规则

报告字段定义见 [schema/test-result.schema.json](../schema/test-result.schema.json)，与上游判定相关的字段：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `upstream_supported` | `true` / `false` | 上游是否原生支持鲲鹏/aarch64 |
| `unsupported_reasons` | 对象数组 | `false` 时至少 1 条原因，结构为 `{code, message, evidence?, checked_at?}`；`true` 时必须为 `[]` |
| `CI_passed` | `true` / `false` | 本次功能测试结论，也是 PR 门禁依据 |
| `run_status` | `completed` / `aborted` / `error` / `force_merged` | 运行级状态 |
| `display` | `true` / `false` | 看板展示开关，仅在 `CI_passed && PR_merged` 时为 `true` |
| `test_cases` | 数组 | 逐条用例结论；未执行到用例阶段时允许为空 |

上游支持的判定矩阵（由 `_judge_upstream_support` 执行）：

| Dockerfile 是否修改上游源码 | 功能测试 | `upstream_supported` | `unsupported_reasons` 中的 message |
| --- | --- | --- | --- |
| 否 | 通过 | `true` | `[]` |
| 否 | 未通过 / 未执行 | `false` | “功能测试未通过” |
| 是 | 通过 | `false` | “Dockerfile 修改了上游源码: line N: ...” |
| 是 | 未通过 | `false` | 以上两条 |

“修改上游源码”的识别规则（`SOURCE_MODIFY_PATTERNS`，逐行匹配、忽略注释行）：

- `git apply`；
- `patch -p<N>` / `patch < xxx.diff`；
- 引用 `.patch` / `.diff` 文件；
- `sed -i`、`perl -i` 原地改源码；
- `COPY` / `ADD` 本地 `src` / `source` / `patches` 目录。

含义：**只要镜像依赖对上游源码打补丁才能运行，就不算“上游原生支持”**，即使测试通过。

## 4. 分阶段判定与处理

### 4.1 构建阶段（check）

- `meta.yml` 中的 `arch` 不包含当前架构：`run_status=aborted`，原因写入 `unsupported_reasons`，**不阻塞**；
- 镜像构建失败：`run_status=error`，**阻塞 PR**，按“镜像构建问题”处理；
- 构建成功：进入测试阶段。

> 当前 x86_64 节点仍走旧流程（发布到 `openeulertest` 后执行 `app check`），只产出门禁结论、不产出报告；aarch64 节点走本地构建 + 镜像内执行 `test.sh` + 报告流程。

### 4.2 测试阶段（check）

| 情况 | 报告结论 | 门禁 |
| --- | --- | --- |
| 未找到 `test.sh` | `CI_passed=false`、`run_status=completed`，原因记为“功能测试未通过”；镜像仍推送供 push 复用 | 不阻塞 |
| 脚本退出码为 0 | `CI_passed=true` | 通过 |
| 脚本退出码非 0 | `CI_passed=false`；若输出中没有 `fail` 用例，把最后一条用例标记为 `fail` | 阻塞 |

用例解析支持两种约定：

1. shUnit2：`testXxx` 独占一行，出现 `ASSERT:` 记为失败；
2. `PASS:` / `FAIL:` / `FATAL:` 行，按 `test_xxx()` 的函数定义顺序一一对应。

### 4.3 合入阶段（push）

| 情况 | 处理 |
| --- | --- |
| 有索引且 `CI_passed=true` | 生成合入报告，`PR_merged=true`、`display=true` |
| 有索引但 `CI_passed=false` | `run_status=force_merged`，`hardware=unknown`、`display=false`，原因“CI 未通过即合入（原测试型号 xxx），需人工确认” |
| 无索引（未跑过 check 或索引已过期） | `run_status=force_merged`，原因“未找到该 PR 的测试报告，按 CI 未通过处理，需人工确认” |
| 该 PR 已存在合入报告 | 覆盖旧报告；若索引已不存在则保留原报告，不降级为 `unknown` |

`force_merged` 是**留痕**，不是通过：报告会写进结果仓库，但不会出现在看板上，必须走人工复核。

## 5. 归因要求（最小证据）

把问题归到上游前，报告中至少要有：

1. 失败的镜像 tag 与硬件型号（`kunpeng920` / `kunpeng950`）、OS 简写；
2. 可直接复现的命令（`docker run ...` 或构建命令）；
3. 关键日志片段（编译错误、崩溃栈、`ASSERT:` 或 `FAIL:` 行）；
4. 上游源码位置或 issue/release 链接。

缺少以上任一项时，只能记为“待确认”，不能写 `upstream_supported=false` 并结束处理。

## 6. 分级、时限与动作

| 级别 | 场景 | 门禁 | 责任动作 | 时限 |
| --- | --- | --- | --- | --- |
| P0 | 构建失败、用例断言失败 | 阻塞 | PR 作者修 Dockerfile / `test.sh`；修好前不允许合入 | 当次 PR 内 |
| P1 | 无测试脚本、`arch` 不支持 | 不阻塞 | 记录原因；维护者决定是否补脚本或声明不支持 | 下次迭代 |
| P2 | `force_merged`（CI 未过仍合入） | 已合入 | CI 维护者复核，补测后覆盖报告 | 合入后 24 小时 |

## 7. 上游沟通与关闭

- 提 issue / PR 时必须带上第 5 节的最小证据，并注明镜像版本与硬件；
- 上游回复或修复后，把链接回填到 `unsupported_reasons[].evidence`（schema 已预留 `code` / `message` / `evidence` / `checked_at` 结构）；
- 关闭条件：上游修复并在新版本验证通过，或上游明确声明不支持；两种情况都要更新对应报告。

## 8. 复核与数据生命周期

- check 阶段写入的 `_pending/<prid>/<应用>/<版本>/<os>.json` 是临时索引，超过 30 天未更新会被清理（PR 关闭未合入）；
- 同一 PR 合入后重跑会覆盖该 PR 原有的合入报告；其他运行按时间戳保留，不覆盖历史；
- 只有 `display=true` 的报告出现在看板；`force_merged`、测试失败、`aborted` 的报告一律不上看板。

## 9. 已知限制（当前实现）

| 限制 | 影响 | 处理方式 |
| --- | --- | --- |
| 服务型镜像被 `--entrypoint bash` 覆盖入口，服务不会启动 | 如 kvrocks 这类镜像测试必然失败，但原因不在上游 | 需要改为 `docker run -d` + `docker exec` 执行用例 |
| `PASS:` / `FAIL:` 与 `test_xxx()` 按定义顺序对齐 | 脚本函数顺序与输出顺序不一致时用例名错位 | 修 `test.sh`，让每条用例只输出一行结果 |
| 无测试脚本时未写 `skip_reason=no_test_script` | 报告中无法直接区分“无脚本”和“测试失败” | 后续在报告模型中补充该字段 |
| 合入报告的实际字段与 schema 未对齐 | `prid` 不在 schema 允许字段内；`unsupported_reasons` 目前写的是纯文本，而 schema 要求 `{code, message}` 对象，导致报告无法通过校验 | 二选一：调整实现（改用对象、`prid` 移入允许字段或删除），或调整 schema |

## 10. 记录示例

上游不支持且测试未通过：

```json
{
  "schema_version": "0.5",
  "software": "redis",
  "version": "8.0.6",
  "hardware": "kunpeng920",
  "os": "oe2403sp4",
  "test_time": "2026-10-28T02:11:05Z",
  "finished_at": "2026-10-28T02:19:41Z",
  "run_status": "completed",
  "display": false,
  "CI_passed": false,
  "PR_merged": true,
  "upstream_supported": false,
  "unsupported_reasons": [
    {
      "code": "FUNCTIONAL_TEST_FAILED",
      "message": "功能测试未通过",
      "checked_at": "2026-10-28T02:19:41Z"
    },
    {
      "code": "UPSTREAM_SOURCE_MODIFIED",
      "message": "Dockerfile 修改了上游源码: line 42: COPY patches/ ./patches/",
      "evidence": ["https://github.com/openeuler/openeuler-docker-images/blob/master/Database/redis/8.0.6/oe2403sp4/Dockerfile"],
      "checked_at": "2026-10-28T02:19:41Z"
    }
  ],
  "test_cases": [
    { "name": "test_start", "description": "redis-server 可启动", "category": "functional", "result": "fail", "test_time": "2026-10-28T02:19:30Z" }
  ]
}
```

## 11. 责任分工

| 角色 | 职责 |
| --- | --- |
| PR 作者 | 修复 Dockerfile / `test.sh`，提供复现信息，确认是否修改了上游源码 |
| CI 维护者 | 维护判定规则、解析逻辑与报告仓库；复核 `force_merged`；推动已知限制修复 |
| 上游维护者 / 社区 | 确认或修复上游缺陷；对“不支持 aarch64”给出明确结论 |
| 看板 / 数据使用方 | 只消费 `display=true` 的报告；`false` 仅用于问题统计与复核 |
