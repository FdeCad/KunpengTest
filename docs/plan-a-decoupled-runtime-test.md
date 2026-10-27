# 鲲鹏功能测试方案（v0.6，Jenkins 无锁 + 归因判定）

| 项目 | 内容 |
|---|---|
| 方案 | 现有 Jenkins 编排 + Agent 执行器并发 + 单队列 FIFO + 归因判定 + 独立发布 job |
| 版本 | v0.7，执行器与用例定义改为复用测试侧仓库现有实现 |
| 目标 | 用现有 Jenkins 完成排队、调度、重试、凭据、日志与反馈，只自研测试与结果转换 |
| 复用 | GitCode PR 触发、Jenkins 构建队列、Agent、凭据、artifact、构建历史 |
| 自研 | 结果归一化、结果 schema 转换、结果仓库、存量播种、覆盖率报告 |
| 不包含 | 结果作为合入门禁、构建逻辑改造（仅追加事件输出）、多 OS 全量矩阵、性能测试 |
| 生成时间 | 2026-10-27 |
| 状态 | 草案 |

---

## 一、复用边界：Jenkins 承担什么

| 能力 | 复用 Jenkins | 自研 |
|---|---|---|
| 事件触发 | 现有 `openeuler-docker-images-check` job（GitCode PR） | 无 |
| 排队 | Jenkins build queue | 无 |
| 调度 / 并发 | Jenkins 执行器数量 | 无 |
| 超时 / 重试 | `timeout` / `retry` | 无 |
| 凭据 | Jenkins Credentials | 无 |
| 日志 / 产物 | build log + `archiveArtifacts` | 无 |
| PR 评论 | GitCode API（复用 `update.py` 的请求封装） | 少量脚本 |
| 测试执行 | Agent 上跑容器 | 复用 `run_func_case.py` + 用例清单，仅补结果归一化 |
| 失败归因 | 独立 Jenkins job，规则文件版本化 | 规则与证据字段 |
| 结果发布 | 独立 job + `disableConcurrentBuilds()` | 少量脚本 |
| 存量 / 兜底 | Jenkins cron job | catalog 与版本策略 |

**结论：不再需要自研 Event Ingress、SQLite 队列和 Scheduler。** 常驻服务从 2 个降为 0 个，新增 5 个 Jenkins job；除发布 job 取 artifact 需要 Copy Artifact 插件（或用 `curl` 走 Jenkins API）外，不需要额外的调度插件。

---

## 二、总体架构

```text
GitCode PR
   │
   ▼
Jenkins: openeuler-docker-images-check（现有）
   ├─ update.py -op check
   │    ├─ 解析 meta.yml → test_tag = <version>-<os>-<arch>
   │    ├─ eulerpublisher publish → docker.io/openeulertest/<app>:<test_tag>
   │    └─ eulerpublisher check（拉回验证）
   └─ 追加 built-images.jsonl
        └─ post-build fan-out：逐条触发测试 job
             │
             ▼
Jenkins: kunpeng-functional-test（新增，参数化）
   ├─ agent { label 'kunpeng920' | 'kunpeng950' }
   ├─ 无锁，并发上限 = 该 agent 的执行器数量 N
   ├─ preflight：架构 / CPU 指纹 / 磁盘 / Docker
   ├─ docker pull <image>@<digest>
   ├─ 执行 func_case.yaml（run_func_case.py，四个 stage）
   └─ archiveArtifacts: result.json, result.log
        └─ build job: kunpeng-triage（不等待）
             │
             ▼
Jenkins: kunpeng-triage（新增）
   ├─ copyArtifacts 取回 result.json
   ├─ 合并用例清单的 integration_type 与构建证据
   ├─ 按 rules/triage.yaml 评估
   └─ archiveArtifacts: result.json, triage.json
        └─ build job: kunpeng-result-publish（不等待）
             │
             ▼
Jenkins: kunpeng-result-publish（新增）
   ├─ copyArtifacts 取回 result.json + triage.json
   ├─ schema 校验 + 路径一致性
   ├─ disableConcurrentBuilds：规范化 + 校验 + git push
   └─ GitCode 评论（advisory，含归因）


***PR合入后触发新的kunpeng-result-publish，新增字段：PR_merged:True/False***



Jenkins cron: kunpeng-backlog-seeder（每 15 分钟，保持待跑队列 ≤ 2N）
Jenkins cron: kunpeng-daily-reconcile（每天，对账 / 覆盖率 / promote）
```

---

## 三、Jenkins 对象设计

### 3.1 Agent

| 主机 | Label | 执行器 | 说明 |
|---|---|---|---|
| hwy | `kunpeng920` | N（先按 4 压测） | 所有来源的任务共用一个执行器池 |
| hwy2 | `kunpeng950` | N | 同上 |

- Agent 用专用系统用户运行，只加入 `docker` 组，不持有 Jenkins 管理员凭据；
- 工作目录固定，`post { always { ... } }` 清理残留容器与临时文件；
- 若不允许在 hwy/hwy2 装 agent，保留降级方案：在编排机上放一个 agent，执行阶段改用 SSH，Job 结构不变（见 §十一）。

### 3.2 并发模型：执行器即上限

不使用 Lockable Resources，也不区分 PR / 存量：

```text
每台机器 N 个执行器
  → 同一时刻最多跑 N 个测试
  → 多出来的任务在 Jenkins 队列里等待，不占执行器
```

| 规则 | 说明 |
|---|---|
| 并发上限 | 由执行器数量决定，`N` 通过并发压测定 |
| 排队 | Jenkins 默认 FIFO，等待发生在队列，不消耗执行器 |
| 互斥 | 不再需要：同一执行器不会同时跑两个任务 |
| 资源隔离 | 由容器 `--cpus / --memory / --pids-limit` 保证，而不是由槽位保证 |

> 与锁模型相比，队列等待不占执行器，也不存在“锁在 job 内等待”造成的执行器浪费。代价是没有 PR 优先级，靠小批量播种控制队列长度（见 §九）。

### 3.3 Jobs

| Job | 触发 | 职责 |
|---|---|---|
| `openeuler-docker-images-check` | GitCode PR（现有） | 构建、推送 `openeulertest`、产出清单、fan-out |
| `kunpeng-functional-test` | fan-out / 播种 | 单镜像 × 单硬件测试，产出结果 JSON |
| `kunpeng-triage` | 测试 job 触发 | 合并用例清单与构建证据，按规则判定失败归因 |
| `kunpeng-result-publish` | triage 触发 | 校验、按契约路径写入结果、PR 评论 |
| `kunpeng-backlog-seeder` | cron 每 15 分钟 | 保持 pending ≤ 2N，小批量播种 |
| `kunpeng-daily-reconcile` | cron 每天 | 对账、覆盖率、promote |

---

## 四、触发链路

### 4.1 构建侧只加两处

1. `update.py::check_updates()` 在 `_check_app_image()` 成功后追加一行 JSONL；
2. `build.sh` 在 `update.py` 退出后读取 JSONL，逐条调用 Jenkins API。

```bash
set +e
sudo env "PATH=..." /usr/bin/python3 update/container/app/update.py ... -op check
rc=$?
set -e

EVENT_FILE="${TEST_EVENT_FILE:-${WORKSPACE:-$PWD}/built-images.jsonl}"
if [ -s "${EVENT_FILE}" ]; then
    while IFS= read -r line; do
        app=$(echo "$line" | jq -r .app)
        tag=$(echo "$line" | jq -r .tag)
        digest=$(echo "$line" | jq -r .digest)
        [ "$(echo "$line" | jq -r .arch)" = "aarch64" ] || continue
        for hw in kunpeng920 kunpeng950; do
            curl -fsS -X POST \
              "${JENKINS_URL}/job/kunpeng-functional-test/buildWithParameters?token=${JENKINS_TOKEN}" \
              --data-urlencode "APP=${app}" \
              --data-urlencode "VERSION=$(echo "$line" | jq -r .version)" \
              --data-urlencode "OS=$(echo "$line" | jq -r .os)" \
              --data-urlencode "TAG=${tag}" \
              --data-urlencode "DIGEST=${digest}" \
              --data-urlencode "HARDWARE=${hw}" \
              --data-urlencode "ORIGIN=pr" \
              --data-urlencode "BUILD_REF=${BUILD_NUMBER}" \
              --data-urlencode "COMMIT=$(echo "$line" | jq -r .commit)" \
              --data-urlencode "PR=$(echo "$line" | jq -r .pr)"
        done
    done < "${EVENT_FILE}"
fi
exit ${rc}
```

构建 job 需要把 `built-images.jsonl` 作为 artifact 归档，供 triage 按 `app + tag + digest` 查询证据。

### 4.2 清单契约

写入点：`update.py::check_updates()`，publish + check 成功后追加。

```jsonl
{"event":"image_built","app":"redis","version":"8.0.6","os":"24.03-lts-sp4","tag":"8.0.6-24.03-lts-sp4-aarch64","arch":"aarch64","digest":"sha256:...","namespace":"openeulertest","meta_file":"Others/redis/8.0.6/24.03-lts-sp4/Dockerfile","source_build":false,"has_patch":false,"base_image":"openeuler/openeuler:24.03-lts-sp4","changed_files":["Others/redis/8.0.6/24.03-lts-sp4/Dockerfile"],"pr":431,"source_repo":"openeuler-docker-images","source_branch":"auto-upgrade/redis-8.0.6","commit":"9f3c1a2b","job":"openeuler-docker-images-check","build_number":"1287","built_at":"2026-10-27T07:12:30Z"}
```

| 字段 | 来源 | 必填 | 用途 |
|---|---|---|---|
| `event` | `image_built` / `build_failed` | 是 | 区分事件 |
| `app` | `format.parse_meta_yml()` | 是 | 结果目录与用例清单定位 |
| `version` | Dockerfile 路径中的版本目录 | 是 | 结果目录，需与目录一致 |
| `os` | Dockerfile 路径中的 OS 目录 | 是 | 映射为结果里的简写（`oe2403sp4`） |
| `tag` | `test_tag` | 是 | 镜像 tag |
| `arch` | `platform.machine()` | 是 | 只触发 `aarch64` |
| `digest` | `docker image inspect` 的 `RepoDigests`，回退 `buildx imagetools` | 是 | 幂等键与 promote 依据 |
| `pr` / `commit` | PR 上下文 | 是 | 旧任务判定、PR 评论 |
| `namespace` | `TEST_NAMESPACE` | 是 | 固定 `openeulertest` |
| `source_build` | Dockerfile 含 `git clone` / `make` / `cmake` / `./configure` | 是 | 归因证据 |
| `has_patch` | PR 含 `.patch`/`.diff`，或 Dockerfile 含 `patch -p1` / `COPY *.patch` / `sed -i` | 是 | 归因证据 |
| `base_image` | 首个 `FROM` | 是 | 判断是否 openEuler base |
| `changed_files` | GitCode PR files API（截断到 50 条） | 是 | 归因证据 |

> digest 必须是 **arm64 单架构 manifest 的 digest**，不能用 `buildx --push` 输出的 list digest。

### 4.3 去重：按目录内 digest 扫描

契约要求文件名是 `<hardware>-<YYYYMMDDTHHMMSSZ>.json`，一个文件一次运行，不能用 digest 做文件名。因此去重改为：

1. 列出 `<A-Z>/<app>/<version>/` 下最近的 JSON（按文件名倒序取前 20 个）；
2. 读取 `extensions.image_digest` 与 `extensions.spec_version`；
3. 与本次相同的 `(digest, hardware, spec_version)` → 跳过；
4. 不同 → 正常触发，产生新文件。

单版本目录通常只有 1–3 个文件，扫描成本可接受；不引入 `index.json`，避免并发写冲突。

---

## 五、测试 Job

### 5.1 参数

`APP` `VERSION` `OS` `TAG` `DIGEST` `HARDWARE` `ORIGIN` `PR` `COMMIT` `BUILD_REF`

> `VERSION` / `OS` 由构建事件显式传入，避免从 tag 反向解析产生歧义；`spec_version` 由 triage 对 `functional-cases/<Category>/<app>/` 下的清单与脚本做内容哈希后写入 `triage.json`。

### 5.2 Pipeline 骨架

```groovy
pipeline {
    agent { label "${params.HARDWARE}" }

    options {
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '200', artifactNumToKeepStr: '200'))
        // 不要加 disableConcurrentBuilds()：并发上限由执行器数量决定
    }

    stages {
        stage('Preflight') {
            steps {
                sh './scripts/preflight.sh "${HARDWARE}"'
            }
        }

        stage('Test') {
            steps {
                // runner 无论成败都写 result.json；退出码只用于 Jenkins 展示
                // 0=执行完成（含功能失败），2=基础设施异常（拉取/启动失败）
                sh '''
                  ./scripts/run-test.sh \
                    --app "${APP}" --version "${VERSION}" --os "${OS}" \
                    --tag "${TAG}" --digest "${DIGEST}" \
                    --hardware "${HARDWARE}" \
                    --output result.json --log result.log
                '''
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'result.json,result.log', allowEmptyArchive: true
            script {
                if (fileExists('result.json')) {
                    build job: 'kunpeng-triage', wait: false, parameters: [
                        string(name: 'RESULT_BUILD', value: "${env.BUILD_NUMBER}"),
                        string(name: 'APP', value: "${params.APP}"),
                        string(name: 'VERSION', value: "${params.VERSION}"),
                        string(name: 'OS', value: "${params.OS}"),
                        string(name: 'TAG', value: "${params.TAG}"),
                        string(name: 'DIGEST', value: "${params.DIGEST}"),
                        string(name: 'HARDWARE', value: "${params.HARDWARE}"),
                        string(name: 'ORIGIN', value: "${params.ORIGIN}"),
                        string(name: 'PR', value: "${params.PR}"),
                        string(name: 'COMMIT', value: "${params.COMMIT}"),
                        string(name: 'BUILD_REF', value: "${params.BUILD_REF}")
                    ]
                }
            }
            sh './scripts/cleanup.sh || true'
        }
    }
}
```

### 5.3 执行步骤

执行器是测试侧仓库里的 Python 脚本，**不是 GitHub Actions workflow**：

```text
docker login --username <bot>              # 认证拉取，避免匿名限流
docker pull <image>@<digest>               # 传播延迟时重试 3 次，60s 退避
run_func_case.py --stage prepare           # 建工作目录、起容器、挂载用例资产
run_func_case.py --stage install           # prebuilt 校验已装软件；build 模式执行构建
run_func_case.py --stage check             # 跑 checks，产出 functional_result.json
run_func_case.py --stage cleanup           # always 执行，强制删除容器与临时目录
```

由 `scripts/run-test.sh` 顺序封装四个 stage：

```bash
python3 functional-framework/run_func_case.py \
  --case "${CASE_PATH}" \
  --version "${VERSION}" --os "${OS}" --hardware "${HARDWARE}" \
  --architecture aarch64 --run-id "${BUILD_NUMBER}" \
  --stage "${STAGE}" --output-root "${OUTPUT_ROOT}"
```

`CASE_PATH` 由 `catalog_func.py` 按 `APP` 从 `functional-cases/**/func_case.yaml` 解析，不硬编码 `specs/<app>.yaml`。

| 项 | 现状（GitHub Actions） | Jenkins 侧 |
|---|---|---|
| 调用方 | `functional-test.yml` 的 cell job | `scripts/run-test.sh` 顺序调用四个 stage |
| 节点选择 | self-hosted runner 标签 `PERF_FUNC_KUNPENG920/950` | agent label `kunpeng920/950`，runner 标签不再参与矩阵 |
| 工作目录 | `work_root: /home/runner/boostkit-func` | 改为测试机本地路径，如 `/var/lib/kunpeng-func` |
| 代理 | Actions Secrets → `PERF_PROXY` | 节点环境变量或 Jenkins 凭据 |
| 用例矩阵 | `catalog_func.py matrix` 生成 | 保留 `catalog_func.py`，由播种 job 与 fan-out 调用 |
| 结果归档 | `actions/upload-artifact` | `run-test.sh` 把 `functional_result.json` 归一化为 `result.json` 后 `archiveArtifacts` |

运行时依赖 `python3` + `PyYAML`；当前 workflow 用 `pip install --target` 临时安装，Jenkins 侧在 agent 上预装或用固定 venv。

**不要用 `workflow_dispatch` 让 Jenkins 去触发 Actions**：

1. Jenkins 需要 GitHub PAT，且拿不到 run 的实时状态，只能轮询；
2. workflow 的 `concurrency: functional-test-runners`（`cancel-in-progress: false`）会把所有测试压成全局单通道；
3. self-hosted runner 必须常驻，等于在 920/950 上装 agent，与 §3.1 的信任边界冲突；
4. Actions runner 与 Jenkins agent 争抢同一台机器，两套系统之间没有共享互斥，会出现同机并发跑测试。

因此：**同一台机器同一时刻只能有一套驱动**。Jenkins 链路接通后关闭 920/950 上的 self-hosted runner，GitHub workflow 仅保留为人工触发与离线验证入口。

---

## 六、用例定义

用例的唯一权威来源是测试侧仓库 `FdeCad/boostkit-performance-evaluation` 的 `functional-cases/`，**不新建 `specs/<app>.yaml`**，也不在本方案里重复定义 `cases/command/expect`：

```text
boostkit-performance-evaluation/
├── config/functional.yaml          # 全局矩阵：OS / hardware / 输出目录
├── functional-framework/
│   ├── catalog_func.py             # 校验清单、生成矩阵、按 app 解析清单路径
│   └── run_func_case.py            # 执行器：prepare / install / check / cleanup
└── functional-cases/
    └── <Category>/<app>/
        ├── func_case.yaml          # 用例清单：版本白名单、阶段脚本、判定项、输出
        ├── build.sh                # func_prepare / func_install
        └── test.sh                 # func_check / func_cleanup
```

清单结构（以 `Database/redis` 为例，节选）：

```yaml
name: redis
category: Database
enabled: true
versions: ["8.10.2", "6.0.20"]      # 人工白名单：最新稳定 + 指定版本，不做 latest 解析
integration_type: openeuler          # 新增：upstream | third_party | openeuler | mixed
upstream_supported: true             # 新增：上游是否支持鲲鹏 / aarch64
unsupported_reasons: []              # 新增：false 时至少一条 {code, message}
execution:
  type: shell-functions
  stages:
    prepare: { script: build.sh, function: func_prepare }
    install: { script: build.sh, function: func_install }
    check:   { script: test.sh,  function: func_check }
    cleanup: { script: test.sh,  function: func_cleanup }
  timeout_minutes: 60
checks:
  - name: version
  - name: upstream-test
    upstream_command: ./runtest --single unit/type/string --clients 5
  - name: cli-roundtrip
outputs:
  functional_result: { path: functional_result.json, stage: check, format: json, required: true }
```

字段约定：

| 字段 | 作用 | 说明 |
|---|---|---|
| `versions` | 版本白名单 | 构建事件的 `VERSION` 不在白名单 → `NO_TEST_SPEC`，不跑 |
| `enabled` | 软件级开关 | `false` 时播种与 fan-out 都不产生任务 |
| `execution.stages` | 四阶段实现 | 由 `run_func_case.py` 按 `--stage` 调用，脚本与清单同目录 |
| `checks` | 判定项 | 名称与 `functional_result.json` 的 `checks[].name` 一一对应 |
| `outputs.functional_result` | 执行器侧结果契约 | Jenkins 侧再归一化为 schema 3.0 的 `result.json` |
| `integration_type` | 归因权威声明 | 由清单维护人填写；Dockerfile 证据只校验、不覆盖 |
| `upstream_supported` / `unsupported_reasons` | 结果字段 | 直接落进 `result.json`，必须满足 schema 一致性约束 |

- **不新增 `applies_to`**：版本范围由 `versions` 表达，避免两套版本语义；
- **`spec_version` 由内容派生**：`sha256(func_case.yaml + build.sh + test.sh)` 取前 12 位，作为幂等键 `digest + hardware + spec_version` 的一部分。用例一改键就变，不需要人工递增；
- **资产与用例同目录**：配置文件、测试数据放 `<Category>/<app>/`，由执行器挂进容器，不依赖镜像内路径或运行期下载；
- **OS 维度**：本期只跑 `config/functional.yaml` 中的 `openeuler-24.03-sp4`（`runtime: container`, `mode: prebuilt`）；`ubuntu-26.04.1` 属 build 模式，不在本期范围；
- **新版本未被白名单覆盖**：不静默跳过，由每日对账输出「已构建但无用例清单」的待补列表，避免自动升级版本长期漏测；
- 环境字段沿用现有 `tests/SKILL.md` 的 11 字段（Model / architecture / os / kernel / cpu_model / cpu_cores 等），由执行器采集，不写进清单；
- 如需保留 `security_profile` 的历史可比性，作为清单可选字段补充，未填时结果中不落该字段。

---

## 七、归因判定与发布

### 7.1 归因判定 Job（kunpeng-triage）

目的：在写结果前判断功能失败是否归因上游。它**不改变 `test_result`**，只产出归因与证据。

> `triage.sh` 必须无论输入是否完整都产出 `triage.json`（默认 `undetermined`），保证发布链路不因归因失败而中断。

输入：

- `result.json`（测试 job 归档）；
- `built-images.jsonl`（构建 job 归档，按 `app + tag + digest` 找到对应记录）；
- `functional-cases/<Category>/<app>/func_case.yaml` 里的 `integration_type` 与 `upstream_supported` / `unsupported_reasons`；
- 版本化的规则文件 `rules/triage.yaml`。

清单路径通过 `catalog_func.py` 按 `APP` 解析（`--app` + `--catalog functional-cases`），不硬编码 `specs/` 路径；`spec_version` 同时由清单目录内容哈希算出并写入 `triage.json`。

```yaml
# rules/triage.yaml
version: 1
rules:
  - id: upstream_or_third_party
    when: { test_result: false, integration_type: [upstream, third_party] }
    verdict: upstream_suspected
    confidence: high
    reason: 镜像只集成上游/第三方产物，未包含 openEuler 侧源码改动

  - id: openeuler_with_patch
    when:
      test_result: false
      integration_type: [openeuler, mixed]
      any_of: [has_patch, source_modified]
    verdict: upstream_suspected
    confidence: medium
    reason: 存在源码改动或 patch，失败可能与上游代码/补丁相关

  - id: openeuler_clean
    when: { test_result: false, integration_type: [openeuler, mixed] }
    verdict: openeuler_suspected
    confidence: medium
    reason: 无源码改动或 patch，优先怀疑 openEuler 集成环境

  - id: pass
    when: { test_result: true }
    verdict: none
    confidence: high

  - id: undetermined
    when: {}
    verdict: undetermined
    confidence: low
```

> `source_modified` 由 `changed_files` 派生：存在 `Dockerfile`、`meta.yml`、`README*` 之外的源码或 patch 文件即为真。

```json
{
  "rule_version": 1,
  "spec_version": "a1b2c3d4e5f6",
  "rule_id": "openeuler_with_patch",
  "verdict": "upstream_suspected",
  "confidence": "medium",
  "reason": "存在源码改动或 patch，失败可能与上游代码/补丁相关",
  "evidence": {
    "integration_type": "openeuler",
    "source_build": true,
    "has_patch": true,
    "source_modified": true,
    "base_image": "openeuler/openeuler:24.03-lts-sp4",
    "changed_files": [
      "HPC/petsc/3.26.0/24.03-lts-sp4/Dockerfile",
      "HPC/petsc/3.26.0/24.03-lts-sp4/fix.patch"
    ],
    "test_result": false
  }
}
```

```groovy
pipeline {
    agent { label 'orchestrator' }
    options { timeout(time: 5, unit: 'MINUTES') }
    parameters {
        string(name: 'RESULT_BUILD')
        string(name: 'BUILD_REF')
        string(name: 'APP')
        string(name: 'VERSION')
        string(name: 'OS')
        string(name: 'TAG')
        string(name: 'DIGEST')
        string(name: 'HARDWARE')
        string(name: 'ORIGIN')
        string(name: 'PR')
        string(name: 'COMMIT')
    }

    stages {
        stage('Collect') {
            steps {
                copyArtifacts projectName: 'kunpeng-functional-test',
                              selector: specific("${params.RESULT_BUILD}"),
                              filter: 'result.json', target: 'incoming'
                copyArtifacts projectName: 'openeuler-docker-images-check',
                              selector: specific("${params.BUILD_REF}"),
                              filter: 'built-images.jsonl', target: 'incoming'
            }
        }
        stage('Evaluate') {
            steps {
                sh '''
                  ./scripts/triage.sh \
                    --result incoming/result.json \
                    --events incoming/built-images.jsonl \
                    --app "${APP}" --tag "${TAG}" --digest "${DIGEST}" \
                    --catalog functional-cases \
                    --rules rules/triage.yaml \
                    --output triage.json
                '''
                sh 'cp incoming/result.json result.json'
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'result.json,triage.json', allowEmptyArchive: true
            script {
                if (fileExists('triage.json')) {
                    build job: 'kunpeng-result-publish', wait: false, parameters: [
                        string(name: 'TRIAGE_BUILD', value: "${env.BUILD_NUMBER}"),
                        string(name: 'APP', value: "${params.APP}"),
                        string(name: 'VERSION', value: "${params.VERSION}"),
                        string(name: 'OS', value: "${params.OS}"),
                        string(name: 'TAG', value: "${params.TAG}"),
                        string(name: 'DIGEST', value: "${params.DIGEST}"),
                        string(name: 'HARDWARE', value: "${params.HARDWARE}"),
                        string(name: 'ORIGIN', value: "${params.ORIGIN}"),
                        string(name: 'PR', value: "${params.PR}"),
                        string(name: 'COMMIT', value: "${params.COMMIT}")
                    ]
                }
            }
        }
    }
}
```

> "上游集成失败 → 上游问题"在归因上是**高置信度判断**，不是数学证明：base image 仍是 `openeuler/openeuler`，glibc、编译器、pip 源都可能引入问题。因此输出用 `upstream_suspected` + `confidence`，业务侧要硬标签时再映射。

### 7.2 发布 Job（kunpeng-result-publish）

目标仓库不是 `FdeCad/KunpengTest`（那是 schema / 示例 / 校验脚本仓），而是：

```text
opensourceways/upstream-dashboard   (private)
└── 分支 KunpengTest
    └── openeuler-docker-images-test/
        └── <A-Z>/<app>/<version>/<hardware>-<YYYYMMDDTHHMMSSZ>.json
```

**Job 参数**

`TRIAGE_BUILD` `APP` `VERSION` `OS` `TAG` `DIGEST` `HARDWARE` `ORIGIN` `PR` `COMMIT`

**Agent 与凭据**

- `orchestrator`：可信节点，持有 `upstream-dashboard` 写权限（GitHub App 优先，PAT 兜底）和 GitCode 评论 token；
- `disableConcurrentBuilds()`：只串行发布，不影响测试并发；
- hwy/hwy2 不持有这些凭据。

**Pipeline**

```groovy
pipeline {
    agent { label 'orchestrator' }
    options {
        disableConcurrentBuilds()
        timeout(time: 15, unit: 'MINUTES')
    }
    environment {
        DASHBOARD_REPO   = 'github.com/opensourceways/upstream-dashboard.git'
        DASHBOARD_BRANCH = 'KunpengTest'
        DASHBOARD_ROOT   = 'openeuler-docker-images-test'
        SCHEMA_REPO      = 'github.com/FdeCad/KunpengTest.git'
    }
    stages {
        stage('Collect') {
            steps {
                copyArtifacts projectName: 'kunpeng-triage',
                              selector: specific("${params.TRIAGE_BUILD}"),
                              filter: 'result.json,triage.json',
                              target: 'incoming'
                sh 'git clone --depth 1 "${SCHEMA_REPO}" schema'
            }
        }
        stage('Normalize') {
            steps {
                sh '''
                  ./scripts/normalize.sh \
                    --result incoming/result.json \
                    --triage incoming/triage.json \
                    --catalog functional-cases \
                    --app "${APP}" --version "${VERSION}" --os "${OS}" \
                    --tag "${TAG}" --digest "${DIGEST}" \
                    --hardware "${HARDWARE}" --origin "${ORIGIN}" \
                    --pr "${PR}" --commit "${COMMIT}" \
                    --job-url "${BUILD_URL}" \
                    --output build/result.json
                '''
            }
        }
        stage('Validate') {
            steps {
                sh '''
                  ./scripts/validate-result.sh \
                    --schema schema/schema/test-result.schema.json \
                    --result build/result.json \
                    --app "${APP}" --version "${VERSION}" --hardware "${HARDWARE}"
                '''
            }
        }
        stage('Dedupe') {
            steps {
                sh './scripts/dedupe.sh --result build/result.json --root "$DASHBOARD_ROOT"'
            }
        }
        stage('Publish') {
            when { expression { fileExists('build/to-publish') } }
            steps {
                withCredentials([usernamePassword(credentialsId: 'upstream-dashboard',
                                                  usernameVariable: 'GH_USER',
                                                  passwordVariable: 'GH_TOKEN')]) {
                    retry(3) {
                        sh '''
                          ./scripts/publish.sh \
                            --repo "$DASHBOARD_REPO" --branch "$DASHBOARD_BRANCH" \
                            --root "$DASHBOARD_ROOT" --result build/result.json
                        '''
                    }
                }
            }
        }
        stage('Comment') {
            steps {
                sh './scripts/comment-pr.sh --result build/result.json --triage incoming/triage.json || true'
            }
        }
    }
    post {
        always {
            archiveArtifacts artifacts: 'build/**,incoming/**', allowEmptyArchive: true
        }
    }
}
```

**归一化映射**（`normalize.sh`）

| 目标字段 | 来源 | 说明 |
|---|---|---|
| `schema_version` | 固定 `"3.0"` | |
| `software` / `version` | `APP` / `VERSION` | 必须与目录一致 |
| `hardware` | `HARDWARE` | `kunpeng920` / `kunpeng950` |
| `os` | `OS` 映射 | `24.03-lts-sp4` → `oe2403sp4`；`ubuntu-26.04.1` → `ub2604` |
| `test_time` / `finished_at` | `result.json` | UTC；`test_time` 决定文件名 |
| `run_status` / `test_result` | `result.json` | 枚举见 schema |
| `upstream_supported` / `unsupported_reasons` | `func_case.yaml` | **不是**失败归因 |
| `test_cases` | `result.json` | 只保留 schema 允许的字段 |
| `job_url` | Jenkins `${BUILD_URL}` | |
| `extensions.image_digest` | `DIGEST` | 去重键 |
| `extensions.image_tag` | `TAG` | |
| `extensions.origin` | `ORIGIN` | `pr` / `canonical` |
| `extensions.pr` / `commit` | 参数 | |
| `extensions.spec_version` | `triage.json` | |
| `extensions.failure_attribution` / `attribution_confidence` / `attribution_rule_id` / `attribution_rule_version` | `triage.json` | 归因只放 extensions |

> `upstream_supported` 的语义是“上游是否支持鲲鹏 / aarch64”，由用例清单提供；失败归因是另一个概念，只能写 `extensions.failure_attribution`。
>
> 表中的 `result.json` 不是执行器原生产物：执行器的 `outputs.functional_result` 是 `functional_result.json`，由测试 job 的 `run-test.sh` 映射为 schema 3.0 的 `result.json`（`checks[]` → `test_cases[]`，整体状态 → `run_status` / `test_result`），发布 job 只认归一化后的 `result.json`。

**文件名与路径**

```text
<DASHBOARD_ROOT>/<shard>/<app>/<version>/<hardware>-<YYYYMMDDTHHMMSSZ>.json
```

- `shard` = 应用名首字母大写；非字母开头归入 `0-9`；
- 时间取 `test_time` 的紧凑 UTC 格式；
- 同一秒多次运行追加 `-<6位run_id>`；
- **一个文件 = 一次运行，不覆盖历史**，不能用 digest 做文件名。

**校验**（`validate-result.sh`）

- 用 `schema/test-result.schema.json` 做 JSON Schema 校验；
- 目录名 = `software` / `version`，文件名前缀 = `hardware`，文件名时间 = `test_time`；
- `os` 必须匹配简写正则；
- `test_result=true` 时不允许 `aborted/error` 或存在 `fail/error` 用例；
- `upstream_supported=false` 时 `unsupported_reasons` 至少 1 条，`true` 时必须为空。

> `FdeCad/KunpengTest` 的 `validate_examples.py` 把路径写死为 `examples/results`，不要直接改它；发布 job 用 schema + 自己的路径检查，或把待校验文件临时放进该仓的 `examples/results` 再跑。

**去重**（`dedupe.sh`）

文件名是时间戳，不能靠路径存在判断。按 version 目录扫描：

```text
1. git ls-tree 列出 <root>/<shard>/<app>/<version>/ 下的 JSON（按文件名倒序取前 20 个）
2. 逐个读取 extensions.image_digest 与 extensions.spec_version
3. 与本次相同 → 判定已发布，不提交，job 成功退出
4. 目录文件数超过阈值（如 50）→ 告警，人工清理
```

单版本目录通常只有 1–3 个文件，扫描成本可以接受。

**推送**

```bash
git clone --depth 1 --branch "${DASHBOARD_BRANCH}" \
  "https://x-access-token:${GH_TOKEN}@${DASHBOARD_REPO}" repo
mkdir -p "repo/${DASHBOARD_ROOT}/${SHARD}/${APP}/${VERSION}"
cp build/result.json "repo/${DASHBOARD_ROOT}/${SHARD}/${APP}/${VERSION}/${FILENAME}"
git -C repo add --all
git -C repo -c user.name='kunpeng-ci' -c user.email='kunpeng-ci@users.noreply.github.com' \
  commit -m "Record ${APP} ${VERSION} ${HARDWARE} ${TEST_TIME}"
git -C repo pull --rebase
git -C repo push
```

**PR 结果放哪里（待定）**

契约只定义了 `openeuler-docker-images-test/` 这个 canonical 目录。PR 阶段的结果有三种选择：

| 方案 | 做法 | 影响 |
|---|---|---|
| A（默认） | 只留 Jenkins artifact + PR 评论，不提交 | 简单，但 artifact 过期后无法 promote |
| B | 同一私有仓新建 `KunpengTest-pr` 分支，根目录 `openeuler-docker-images-test-pr/` | 可持久 promote，推荐；需仓库 owner 同意 |
| C | 写进 canonical 目录并用 `extensions.origin=pr` 区分 | 看板会看到未合入结果，不推荐 |

合入时因为 manifest 合并、digest 不变，B 方案可以直接把 PR 结果复制到 canonical 路径，不需要重测。

**失败处理**

| 失败 | 行为 |
|---|---|
| schema / 路径校验失败 | job 失败，不推送，artifact 保留 |
| 去重命中 | job 成功，不产生提交 |
| push 重试 3 次仍失败 | job 失败，artifact 保留并告警 |
| PR 评论失败 | 只告警，不影响已发布结果 |
| `triage.json` 缺失 | 归因写 `undetermined`，照常发布 |

> `copyArtifacts` 需要 Copy Artifact 插件；若不允许安装，改为用 `curl` 从 Jenkins artifact URL 拉取 `result.json` 和 `triage.json`。

---

## 八、幂等与旧任务

| 场景 | 键 / 处理 |
|---|---|
| 是否已测 | `digest + hardware + spec_version`，扫描版本目录内 JSON 的 `extensions.image_digest` |
| PR 新提交 | 新 `commit` 生成新任务；旧结果保留但不提升 |
| 旧任务晚到 | 发布前比对 PR 当前 head，非最新直接丢弃（避免 abort 误伤其他 PR） |
| 基础设施 error | runner 内部重试拉取；仍失败写 `run_status=error`，由每日对账重试，最多 3 次 |
| 功能失败 | 不自动重测，除非 digest / 用例清单变化或人工触发 |

> 用“发布时校验 commit 是否最新”代替“abort 旧构建”。参数化 job 共用同一个 job，`abortPrevious` 会误杀其他 PR 的构建。

---

## 九、并发与容量

```text
并发 = 2 台机器 × N 个执行器
日产能 = 2N × (1440 / 单次分钟)
```

| N（每机） | 总并发 | 12 分钟/次 | 理论日产能 |
|---|---|---|---|
| 2 | 4 | 20 次/小时 | 480 |
| 4 | 8 | 40 次/小时 | 960 |
| 6 | 12 | 60 次/小时 | 1440 |

`N` 先压测再定：同一台机器分别跑 1/2/4/6 并发同一用例清单，取 p95 耗时膨胀 < 20% 且失败率为 0 的最大值。

| 场景 | 测试数 | N=4 时消化时间 |
|---|---|---|
| 存量（每 app × OS 最新 + pinned） | 600–1000 | 约 1–1.5 天 |
| 日增 30 PR × 2 硬件 | 60/天 | 约 1.5 小时 |
| 日增 80 PR × 2 硬件 | 160/天 | 约 4 小时 |

- 单队列 FIFO，不区分 PR / 存量；
- `kunpeng-backlog-seeder` 每 15 分钟检查 `/queue/api/json`，保持 pending ≤ 2N，避免 PR 被长队列拖住；
- 无锁意味着没有 PR 优先级：pending ≤ 2N 时，PR 最坏等待约 2 个测试时长；若不满足，再加 Priority Sorter，调度模型不变；
- Jenkins 队列在 controller 重启后会丢失，播种 job 必须可重复执行，每日对账兜底。

---

## 十、失败处理

| 失败 | 处理 | 结果 |
|---|---|---|
| 构建失败 | 不入队 | PR 评论“构建失败，未测试” |
| fan-out 投递失败 | 清单保留在构建节点，播种 job 兜底 | 15 分钟内补触发 |
| Agent 离线 | Jenkins 队列等待；超过阈值告警 | 恢复后自动执行 |
| 拉镜像失败 | runner 内部重试 3 次（60s 退避） | `run_status=error` |
| 容器/用例超时 | `timeout` + 容器硬超时 | `fail` 或 `error` |
| 发布冲突 | `disableConcurrentBuilds` + rebase 重试 3 次 | 仍失败则保留 artifact 并告警 |
| triage 规则执行失败 / 证据缺失 | 写 `undetermined`，不阻断发布 | 结果照常落库，归因待人工复核 |
| 用例清单缺失 | 不测 | `NO_TEST_SPEC` |
| 旧 commit 结果晚到 | 丢弃，不写 canonical | 无 |

---

## 十一、Agent 不可安装时的降级

如果 hwy/hwy2 不能装 Java agent，保留同一套 job 结构，只把执行阶段换成 SSH：

```groovy
stage('Test') {
    steps {
        sshCommand remote: [name: "${HARDWARE}", ...], command: """
            ./run-test.sh --app ${APP} --digest ${DIGEST} ...
        """
    }
}
```

- 需要 `SSH Pipeline Steps` 插件和 Jenkins 凭据；
- 排队、并发上限、日志仍然由 Jenkins 提供；
- 好处是 worker 不持有 Jenkins 凭据，PR 镜像逃逸的影响范围更小。

---

## 十二、安全

- PR 测试只接受白名单来源（自动升级机器人 / `auto-upgrade` 标签），其余拒绝；
- 不区分 lane 后 PR 与存量共用执行器池；若 PR 镜像的信任边界不可接受，应在 **agent 级别**隔离（独立主机或 VM），而不是在槽位级别区分；
- 容器默认 `--network none`、`--cap-drop ALL`、`no-new-privileges`；
- `functional` 档保留 `CHOWN,SETUID,SETGID,DAC_OVERRIDE` 并配 tmpfs，否则服务类镜像启动即失败；
- 禁止 `--privileged`，不设例外；
- 结果仓库凭据只放在 `orchestrator` 节点，hwy/hwy2 不持有；
- Docker 18.09 已 EOL，不允许作为不可信镜像的长期隔离边界，需排期升级或改用独立 VM。

---

## 十三、可观测性

优先用 Jenkins 自带能力：

| 指标 | 来源 |
|---|---|
| 队列深度 / 等待时间 | `/queue/api/json` |
| 单次耗时 / 成功率 | Jenkins build history |
| 结果分布 | 结果文件 + 每日报告 |
| 失败归因分布 | `extensions.failure_attribution` 聚合 |
| Agent 在线状态 | Jenkins node API |

告警：队列深度持续增长、PR 任务排队等待 > 15 分钟、Agent 连续离线、发布 job 连续失败、`NO_TEST_SPEC` 比例上升、`undetermined` 归因占比上升。

---

## 十四、MVP 范围

**包含**

- 只测 aarch64 镜像，OS 从 `24.03-lts-sp4` 起步；
- 只测 `kunpeng920` / `kunpeng950`；
- PR 触发 + 存量播种 + 每日对账；
- 每 app × OS 最新版 + pinned 清单；
- PR 结果评论（advisory），不阻塞合入；
- 失败归因（advisory），写入结果并在评论中展示，不阻断发布。

**不包含**

- 结果作为合入门禁；
- 改动构建逻辑（仅追加清单输出与 fan-out）；
- 多 OS 全量矩阵、性能测试。

---

## 十五、落地计划

| 阶段 | 内容 | 验证 |
|---|---|---|
| 第 1 周 | hwy 注册 agent（N 个执行器）、建测试 job、跑通 redis | 手工参数触发，产出合法 JSON |
| 第 2 周 | 接入 hwy2、triage job、结果发布 job、PR 评论 | PR 构建成功 → 15 分钟内看到两条结果与归因 |
| 第 3 周 | `update.py`/`build.sh` 清单输出与 fan-out、按目录内 digest 去重 | 一个 PR 自动触发，不重复，归因字段正确 |
| 第 4 周 | 存量播种、每日对账、告警与容量调优 | 存量按天消化，PR 等待可控 |

---

## 十六、验收标准

1. PR 构建并推送 `openeulertest` 成功后，p95 ≤ 15 分钟在 PR 上看到两台硬件结果。
2. 没有新镜像时，不产生测试构建，也不产生结果提交。
3. 同 digest + 同用例清单不重复测试；PR 新提交不覆盖旧结果。
4. 合入采用 manifest 合并，arm64 digest 不变，直接提升为 canonical，不重测。
5. 存量播种期间，PR 任务在 Jenkins 队列中的 p95 等待 ≤ 10 分钟。
6. fan-out 丢失后 15 分钟内由播种 job 补齐，且不产生重复结果。
7. 非法 JSON、路径不一致的结果不进入结果仓库。
8. 所有结果通过 KunpengTest schema 3.0 校验。
9. 上游/第三方集成失败 → `upstream_suspected`；openEuler 集成有源码改动或 patch → `upstream_suspected`；无改动 → `openeuler_suspected`。
10. triage 执行失败或证据缺失时，结果仍能发布，归因为 `undetermined`。

---

## 十七、风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| Jenkins controller 重启丢队列 | 待执行任务消失 | 播种 job 幂等，每 15 分钟补足 |
| Agent 装在测试机 | PR 镜像逃逸触及 Jenkins 凭据 | 专用用户 + 不给管理员凭据 + 独立 agent/VM；或改用 SSH 模式 |
| 参数化 job 共用 | abort 旧构建会误伤其他 PR | 不 abort，发布时校验 commit 是否为最新 |
| 大量排队拖慢 controller | UI 卡顿、调度变慢 | pending ≤ 2N，分批播种 |
| 无锁下长任务占满执行器 | 吞吐下降 | 硬超时 + 提前预拉取 + 每日对账补救 |
| PR 无优先级 | 被存量拖慢 | pending ≤ 2N；仍不满足再加 Priority Sorter |
| 归因规则误判 | 责任划分错误 | 输出 `confidence` + `evidence`，规则版本化；先影子运行，不接门禁 |
| 用例清单与 Dockerfile 证据冲突 | 归因偏差 | 清单声明优先，冲突记录到结果并标 `undetermined` 待复核 |
| 证据缺失（JSONL 未归档） | 无法判定 | triage 写 `undetermined`，不阻断发布 |
| 构建脚本 `prune -af --volumes` | 清空节点镜像与卷 | 改为 `docker image prune -f` |
| `check_updates` 首个失败即返回 | 同 PR 后续镜像不构建 | 已产出记录仍 fan-out，失败项标 `BUILD_FAILED` |
| Docker Hub 限流 | 拉取失败 | 认证拉取 + pull-through cache；必要时升级套餐 |
| 结果仓库持续增长 | clone 变慢 | 按版本目录保留 + PR 结果 30 天清理 + 只 `ls-tree` 遍历 |
| 无用例清单的镜像被误判 | 数据失真 | `NO_TEST_SPEC` 单列，覆盖率区分“未测”和“通过” |

---

## 十八、待确认项

1. hwy/hwy2 能否安装 Jenkins agent（JDK、回连 controller、专用用户）？
2. Jenkins 是否允许新建 job 与安装 Copy Artifact 插件；（SSH 降级另需 `SSH Pipeline Steps`）
3. 合入后的 `-op push` 由哪个 job 触发？确认仍使用 `-s + -m` 做 manifest 合并。
4. `openeulertest` 是 Docker Hub 的 Personal 还是付费组织？决定拉取限流。
5. GitCode 评论是否支持更新既有评论（否则按 commit 追加）。
6. PR 白名单按作者、标签还是 `source_repo`？
7. 结果仓库地址与分支、`orchestrator` 节点的归属与凭据。
8. 首批覆盖的软件清单与 `pinned.yaml`、`version.yaml` 初始内容。
9. `integration_type` 的初始映射由谁维护，`rules/triage.yaml` 的评审流程是什么。
10. 归因结论是否在 PR 评论中显式展示，还是只写进结果 JSON。
11. 920/950 上的 GitHub self-hosted runner 何时关闭？关闭后是否保留一个手动入口做对照验证。
12. 执行器在 Jenkins agent 上的 `work_root`、`output_root` 落点与磁盘配额，以及 PyYAML 的安装方式（系统包还是固定 venv）。
