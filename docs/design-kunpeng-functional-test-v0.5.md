# 鲲鹏镜像功能测试设计方案 v0.5

| 项目 | 内容 |
|---|---|
| 版本 | 0.5 |
| 状态 | 设计稿 |
| 日期 | 2026-10-28 |
| 取代 | `plan-a-decoupled-runtime-test.md`（旧草案保留，不再维护） |
| 核心变化 | 测试内嵌到 aarch64 子 job；测试脚本随 PR 走；结果无条件落库；测试结果作为 CI 门禁；合入后重跑一次 |

---

## 一、目标与非目标

### 1.1 目标

1. trigger job 保持不变，不引入新的事件接收端和服务。
2. aarch64 子 job 构建并推送镜像成功后，**立即在同一 job 内**执行功能测试。
3. 功能测试脚本随 PR 提交，默认位于仓库根目录（与 README 同级）；PR 未提供时回退到仓库既有脚本。
4. 测试后执行上游归因，无论通过与否都生成结果并推送到结果仓库。
5. 测试通过 → CI 通过；测试失败 → CI 阻塞。
6. PR 合入后再跑一次测试，结果写入 canonical 目录。

### 1.2 非目标

- 存量镜像批量回填（本期只做 PR 与合入两条路径，存量用参数化手工触发）。
- 性能测试。
- 多 OS 全量矩阵（本期只覆盖该构建 job 对应的 OS）。
- 结果作为看板以外的二次消费（门禁只认 CI 结论）。

---

## 二、与旧草案的差异

| 维度 | 旧草案 | v0.5 |
|---|---|---|
| 触发点 | 构建 job 写 `built-images.jsonl`，fan-out 到独立测试 job | aarch64 子 job 内直接串一个 test stage |
| 执行位置 | 920/950 专用 agent | aarch64 子 job 所在节点（专用测试节点） |
| 用例来源 | 测试侧仓库 `functional-cases/**/func_case.yaml` | PR 提交中的根目录 `test.sh`，缺失时回退仓库版本 |
| 门禁语义 | advisory，不阻塞 | 阻塞：失败即 CI 失败 |
| 结果推送 | 仅成功/失败都推，但发布由独立 job 异步做 | 仍然独立 job 推送，但测试失败也必须推 |
| 幂等键 | `digest + hardware + spec_version` | `digest + hardware + test_script_sha + PR_merged` |
| 合入后 | 复用 PR 结果 promote，不重测 | 合入后重跑一次，写 canonical |
| 存量 | 定时播种 + 每日对账 | 不在本期，改为手工参数化触发 |

移除的组件：`built-images.jsonl`、独立 `kunpeng-functional-test` job、backlog seeder、按 `spec_version` 去重、双硬件静态分槽。

---

## 三、总体架构

```text
GitCode PR
   │
   ▼
trigger job（现有，完全不变）
   ├─ Groovy：abort 同 PR 旧构建
   └─ Bash：打 ci_processing label → ac.py（仅 ACL）
        │
        └─ downstream
             ├─ comment
             ├─ x86
             └─ aarch64 构建 job（改动点）
                  ├─ build + push（现有）
                  ├─ cleanup credentials        ← 新增：测试前清掉推送凭据
                  ├─ stage: Kunpeng Functional Test   ← 新增
                  │    ├─ 解析测试脚本（PR → 仓库回退）
                  │    ├─ 容器内对镜像执行
                  │    └─ 产出 result.json / result.log
                  ├─ stage: Triage             ← 新增：上游归因
                  ├─ post: build → kunpeng-result-publish（wait: false，always）
                  └─ 测试失败 → job FAILURE → 门禁阻塞
```

结果推送在独立 job 执行，凭据只存在于可信节点，不落在执行 PR 脚本的机器上。

---

## 四、执行流程

### 4.1 aarch64 子 job 的 stage 顺序

```text
1. Build / Push          （现有逻辑，不改）
2. Cleanup Credentials   （docker logout、清理临时 DOCKER_CONFIG、unset 密码型环境变量）
3. Resolve Test Script   （PR 提交 → 仓库回退 → NOT_APPLICABLE）
4. Run Functional Test   （容器内执行，超时硬约束）
5. Triage                （规则归因，失败也执行）
6. Archive + Publish     （post always，异步投递发布 job）
```

关键约束：

- **必须在推送之后、测试之前清理凭据**。PR 提供的脚本视为不可信代码，节点上不能同时存在 registry 写凭据。
- 测试 stage 使用专用非特权系统用户执行，容器参数固定：`--rm`、`--pids-limit`、`--memory`、`--cpus`、`--security-opt no-new-privileges`，默认禁用网络，只有脚本显式声明 `NETWORK=1` 时才放开。
- 超时双层：Jenkins `timeout` + 容器硬超时，超时按 `error` 处理并强制清理。

### 4.2 测试脚本解析

固定文件名与路径：

```text
<repo>/test.sh          # 与 README.md 同级
```

解析顺序：

| 顺序 | 来源 | 条件 | 标记 |
|---|---|---|---|
| 1 | PR head 的 `test.sh` | 文件中存在 | `source=pr` |
| 2 | 目标分支的 `test.sh` | PR 中不存在或被删除，`git show origin/<target>:test.sh` 取到 | `source=repo` |
| 3 | 无 | 两处都不存在 | `NOT_APPLICABLE` |

- `NOT_APPLICABLE` **不阻塞**，结果里记 `CI_passed=true`、`run_status=completed`、`skip_reason=no_test_script`，同时计入覆盖率缺口，避免把没有脚本的仓库全部卡死。
- 回退取的是**目标分支版本**，不是 PR 修改后的版本，防止作者删掉脚本绕过测试。

### 4.3 脚本契约

```bash
#!/bin/bash
# 由 Jenkins 注入的环境变量：
#   IMAGE       完整镜像地址（含 tag）
#   DIGEST      arm64 manifest digest
#   APP VERSION OS ARCH HARDWARE
#   WORKDIR     结果输出目录（可写）

set -euo pipefail
docker run --rm "${IMAGE}" redis-cli --version
```

| 项 | 约定 |
|---|---|
| 退出码 | `0` = 通过；非 `0` = 失败并阻塞 CI |
| 标准输出/错误 | 收集到 `result.log`，作为 artifact 保留 |
| 可选报告 | `$WORKDIR/test-report.json`，格式 `{"cases":[{"name":"...","result":"pass|fail|skip","message":"..."}]}`；存在则并入结果 `test_cases`，不存在则整体记一条 `functional` |
| 超时 | 默认 30 分钟，可用 job 参数覆盖 |

---

## 五、结果生成与推送

### 5.1 result.json

字段全部为顶层字段，不使用 `extensions` 嵌套。当前 KunpengTest schema 版本为 0.5（与 eulerpublisher 实现保持一致）：

| 字段 | 类型 | 来源 |
|---|---|---|
| `schema_version` | string | 固定 `"0.5"` |
| `software` / `version` | string | `APP` / `VERSION` |
| `hardware` | string | 节点指纹（`kunpeng920` / `kunpeng950`） |
| `os` | string | 构建 job 的 `OS` 简写 |
| `test_time` / `finished_at` | string | test stage 起止时间（UTC） |
| `run_status` | string | `completed` / `aborted` / `error` / `force_merged` |
| `CI_passed` | bool | 门禁结论：脚本退出码与用例结果 |
| `PR_merged` | bool | 本次运行是否来自合入后重跑 |
| `image_digest` / `image_tag` | string | `DIGEST` / tag |
| `upstream_supported` | bool | 仓库既有声明，缺省 `true` |
| `unsupported_reasons` | array | `upstream_supported=false` 时至少 1 条，否则 `[]` |
| `test_cases` | array | `test-report.json` 或单条 `functional` |
| `test_script_source` / `test_script_sha` | string | `pr` / `repo` / `none`，及脚本 sha256 前 12 位 |
| `skip_reason` | string | 可选，仅 `NOT_APPLICABLE` 时出现，固定 `no_test_script` |
| `pr` / `commit` | int / string | PR 上下文 |
| `failure_attribution` / `attribution_confidence` / `attribution_rule_id` | string | triage 输出 |
| `job_url` | string | `${BUILD_URL}` |

PR 通过的最小示例：

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
  "CI_passed": true,
  "PR_merged": false,
  "image_digest": "sha256:9f3c...",
  "image_tag": "8.0.6-24.03-lts-sp4-aarch64",
  "upstream_supported": true,
  "unsupported_reasons": [],
  "test_cases": [{ "name": "functional", "result": "pass" }],
  "test_script_source": "pr",
  "test_script_sha": "a1b2c3d4e5f6",
  "pr": 431,
  "commit": "9f3c1a2b",
  "failure_attribution": "none",
  "attribution_confidence": "high",
  "attribution_rule_id": "pass",
  "job_url": "https://ci.example/job/openeuler-docker-images-build-aarch64/1287/"
}
```

### 5.2 推送目标

| 场景 | 分支 | 根目录 |
|---|---|---|
| PR（通过或失败） | `KunpengTest-pr` | `openeuler-docker-images-test-pr/` |
| 合入后重跑 | `KunpengTest` | `openeuler-docker-images-test/` |

```text
<root>/<A-Z>/<app>/<version>/<hardware>-<YYYYMMDDTHHMMSSZ>.json
```

- 一个文件 = 一次运行；同一 PR 合入后重跑会覆盖该 PR 原有的合入报告，其他运行按时间戳各自保留；
- 去重键：`image_digest + hardware + test_script_sha + PR_merged`，命中则覆盖原报告而不新增文件；
- 推送 job 串行执行（`disableConcurrentBuilds`），冲突时 rebase 重试 3 次；
- **推送失败不影响门禁结论**，只告警并保留 artifact，避免结果仓故障阻塞所有 PR。

---

## 六、上游归因

test stage 之后、推送之前执行，规则与旧草案一致（`rules/triage.yaml` 版本化）：

| 条件 | verdict | 置信度 |
|---|---|---|
| 测试通过 | `none` | high |
| 失败 + 集成类型为 upstream / third_party | `upstream_suspected` | high |
| 失败 + 集成类型为 openeuler / mixed 且有源码改动或 patch | `upstream_suspected` | medium |
| 失败 + 集成类型为 openeuler / mixed 且无改动 | `openeuler_suspected` | medium |
| 证据缺失或规则未命中 | `undetermined` | low |

- 归因**不改变** CI 结论，只写入结果与 PR 评论，供人工判断；
- 集成类型取自仓库内的声明文件，Dockerfile 证据仅用于校验。

---

## 七、门禁语义

| 测试结局 | `run_status` | `CI_passed` | aarch64 job | CI |
|---|---|---|---|---|
| 通过 | `completed` | `true` | SUCCESS | 通过 |
| 失败 | `completed` | `false` | **FAILURE** | **阻塞** |
| 无测试脚本 | `completed` | `true` + `skip_reason` | SUCCESS | 通过（标注未测） |
| 脚本超时 / 环境异常 | `error` | `false` | FAILURE | 阻塞 |
| 拉镜像失败（已重试 3 次） | `error` | `false` | FAILURE | 阻塞 |

- 用 `FAILURE` 而不是 `UNSTABLE`，确保下游聚合一定判失败；
- stage 名称固定为 `Kunpeng Functional Test`，PR 评论要区分「构建失败」与「功能测试失败」，避免误导；
- 结果推送 job 的成败不计入门禁。

---

## 八、合入后重跑

- 合入触发同一条流水线（现有合入构建），在推送 manifest 之后执行与 PR 相同的 test stage，结果中 `PR_merged=true`；
- 结果写入 canonical 目录，供看板消费；PR 阶段的结果 `PR_merged=false`，两者靠该字段区分，不再使用 `origin`；
- 合入构建不重新构建镜像，digest 不变，但按本方案要求仍重跑一次；
- 重跑失败不回滚合入，只更新看板与告警（合入后无 PR 可阻塞）。

---

## 九、失败处理

| 失败 | 行为 |
|---|---|
| 测试脚本不存在 | `NOT_APPLICABLE`，不阻塞，计入覆盖率缺口 |
| 测试脚本执行失败 | 结果照常生成并推送，job FAILURE，CI 阻塞 |
| 测试脚本超时 | 强制清理容器，`run_status=error`，job FAILURE |
| 测试脚本试图访问网络 | 默认无网络；需要时由脚本声明 `NETWORK=1` |
| triage 失败 | 写 `undetermined`，不影响门禁与推送 |
| 结果推送失败 | 告警 + 保留 artifact，门禁不变 |
| 合入后重跑失败 | 看板标红 + 告警，不回滚 |

---

## 十、安全边界

1. **PR 脚本是不可信代码**：执行节点必须是与构建凭据隔离的专用节点，测试前清理 registry 登录态与临时凭据。
2. **脚本享有 Docker 权限即等价 root**：这是本方案接受的残余风险，控制手段是节点隔离（无凭据、无内网访问、可重建），而不是容器内沙箱。
3. **结果推送凭据不上测试节点**：推送在独立可信 job 完成。
4. **脚本来源可审计**：结果里记录 `test_script_source` 与 `test_script_sha`，回退到仓库版本时明确标注。
5. **禁止从 PR 读取任何凭据文件**：测试容器不挂载 Jenkins workspace 之外的路径。

---

## 十一、验收标准

1. trigger job 无任何改动，PR 仍能正常触发门禁。
2. aarch64 构建成功后 1 分钟内进入 test stage，不依赖额外调度组件。
3. PR 提供 `test.sh` 时使用 PR 版本，未提供时使用目标分支版本，二者都记录在结果中。
4. 测试通过 → PR 显示门禁通过；测试失败 → PR 阻塞且评论区分「构建失败」与「测试失败」。
5. 无论通过或失败，结果都出现在 `KunpengTest-pr` 分支对应版本目录。
6. 合入后自动重跑一次，结果写入 `KunpengTest` canonical 目录。
7. 同一 `digest + hardware + test_script_sha + PR_merged` 不产生重复结果文件。
8. 结果 JSON 通过 KunpengTest schema 0.5 校验（新增 `CI_passed` / `PR_merged`，删除 `extensions`）。

---

## 十二、待确认项

1. aarch64 子 job 所在节点是否为专用鲲鹏机器？如果是共享构建节点，必须先解决凭据残留与节点复用风险。
2. 该节点能否覆盖 920 与 950 两代硬件？若只覆盖一代，另一代只能由独立 job 或后续扩展补齐。
3. 合入构建在环境变量上如何区分（`gitcodeAction`、分支推送、还是 `-op push` 参数）？
4. 是否存在现成的 `comment` job 聚合逻辑？需要确认测试失败时它能读到 aarch64 job 的 FAILURE。
5. `upstream_supported` 的权威声明放在哪里（`meta.yml` 还是仓库根独立文件）？
6. 测试脚本文件名是否固定为 `test.sh`，还是允许按软件名（如 `redis-test.sh`）？
7. `KunpengTest-pr` 分支的创建需要 `upstream-dashboard` 仓库 owner 同意。
8. KunpengTest schema 升级到 0.5（删 `extensions`、新增 `CI_passed` / `PR_merged`），看板与校验脚本需要同步改，由谁维护？
9. `CI_passed` 与 `run_status` 的组合语义是否要固化到 schema 校验规则（例如 `run_status=error` 时必须 `CI_passed=false`）？
