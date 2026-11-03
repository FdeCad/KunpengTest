# 上游问题分析与处理机制

> 适用对象：`openeuler/openeuler-docker-images` 在鲲鹏（Kunpeng 920 / 950）上的容器镜像 CI 测试与报告链路。
> 对应实现：`update/container/app/update.py`、`report.py`、`source_rules.py`（check / push 两个阶段）、`schema/test-result.schema.json`（报告契约，当前 `0.5`）。

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
| `unsupported_reasons` | 字符串数组 | `false` 时至少 1 条原因；`true` 时必须为 `[]`。schema 中预留了 `{code, message, evidence?, checked_at?}` 对象结构，当前 CI 实现写的是纯文本（见第 9 节） |
| `CI_passed` | `true` / `false` | 本次功能测试结论，也是 PR 门禁依据 |
| `run_status` | `completed` / `aborted` / `error` / `force_merged` | 运行级状态；`force_merged` 仅见于历史数据 |
| `display` | `true` / `false` | 看板展示开关，仅在 `CI_passed && PR_merged` 时为 `true` |
| `test_cases` | 数组 | 逐条用例结论；未执行到用例阶段时允许为空 |
| `prid` | 字符串 | 产生该报告的 PR 编号，check 与合入报告都会写入 |

上游支持的判定矩阵（由 `_judge_upstream_support` 执行）：

| Dockerfile 是否修改上游源码 | 功能测试 | `upstream_supported` | `unsupported_reasons` 中的 message |
| --- | --- | --- | --- |
| 否 | 通过 | `true` | `[]` |
| 否 | 未通过 / 未执行 | `false` | “功能测试未通过” |
| 是 | 通过 | `false` | “Dockerfile 修改了上游源码: line N: ...” |
| 是 | 未通过 | `false` | 以上两条 |

### 3.1 “修改上游源码”的识别规则

判定口径：**只统计上游项目源码树内的实际改动**；改系统配置、测试配置、镜像内的脚手架工程都不算。

行级正则（`SOURCE_MODIFY_PATTERNS`，逐行匹配、忽略注释行）：

- `git apply`；
- `patch -p<N>` / `patch < xxx.diff`；
- 引用 `.patch` / `.diff` 文件；
- `perl -i` 原地修改。

指令解析（`source_rules.py`，避免“整行正则”把路径或配置误判成源码）：

| 指令 | 规则 |
| --- | --- |
| `COPY/ADD` | 带 `--from=<stage>` 的阶段间拷贝一律不算 |
| `COPY/ADD` | 源路径含 `src` / `source` / `patches` 路径段 → 算（本地源码或补丁覆盖） |
| `COPY/ADD` | 目标是绝对路径且位于 `/src/`、`/source/` → 算（把本地文件覆盖进上游源码树，例如 `COPY make.inc /fvcom/src/make.inc`） |
| `COPY/ADD` | 目标命中 `/src/main/`、`/src/test/` → 不算（Maven/Gradle 脚手架示例工程） |
| `sed -i` | 解析被编辑的文件；目标是 `/etc/`、`conf/`、`.conf`/`.cfg`/`.yaml`/`.yml`/`.xml`/`.json` 等配置 → 不算 |
| `sed -i` | `build.xml`、`pom.xml` 例外，仍算构建文件改动 |
| `sed -i` | 目标写在续行上、无法判断时，保守地算命中 |

含义：**只要镜像依赖对上游源码打补丁才能运行，就不算“上游原生支持”**，即使测试通过。

### 3.2 规则的验证结果与已知边界

2026-11-02 基于看板上的 1548 个 Dockerfile 做过一次分层抽样（50 个）验证：

| 规则 | 抽样精确率（不含边界） | 主要误报/漏报 |
| --- | --- | --- |
| `.patch` / `.diff` | 100% | 仅新增文档类补丁属于边界 |
| `sed -i` | 80% | 已收窄配置类目标（neo4j、nagios 等） |
| `COPY/ADD` | 16.7%（收窄前） | 目标路径里的 `src/main/java` 被误判；收窄后该类误报消除 |

收窄规则后全仓对比：**101 个文件由“命中”变为“不命中”，0 个新增命中**，逐条核对均为配置修改或脚手架拷贝。实际效果可参考 eino 0.10.0-alpha.34（PR !5488）：收窄前合入报告因 `COPY --from=builder /src/eino /eino` 被判 `upstream_supported=false`，收窄后重新判为 `true`。

已知漏报（规则尚未覆盖，遇到时需人工判断）：

- 源码树内写入：`echo ... > /path/src/...`、`cat > ...`；
- 删除后重新生成：`rm -rf src/... && swagger generate ...`；
- 格式化工具：`mvn spotless:apply`；
- Dockerfile `COPY` 进去的构建脚本里再执行 `patch` / `git apply`。

## 4. 分阶段判定与处理

### 4.1 构建阶段（check）

- `meta.yml` 中的 `arch` 不包含当前架构：`run_status=aborted`，原因写入 `unsupported_reasons`，**不阻塞**；
- 镜像构建失败：`run_status=error`，**阻塞 PR**，按“镜像构建问题”处理；
- 构建成功：进入测试阶段。

> x86_64 节点仍走旧流程（发布到 `openeulertest` 后执行 `app check`），只产出门禁结论、不产出报告；aarch64 节点走本地构建 + 镜像内执行 `test.sh` + 报告流程，且不在 aarch64 上执行仓库格式检查。

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

check 阶段把报告直接写进正式目录，并在报告里写入 `prid`；push 阶段按 `prid` 找到该 PR 的 check 报告再生成合入报告：

| 情况 | 处理 |
| --- | --- |
| 找到该 PR 的 check 报告 | 复制生成 `...-merged.json`：`PR_merged`、`CI_passed`、`display` 置为 `true`，`run_status=completed`，`test_cases` 原样继承，`upstream_supported` 按合入的 Dockerfile 重新判定 |
| 未找到 check 报告 | 不生成任何文件，只记录告警；需补跑 check 后重新触发 push |
| 同一 PR 重复合入 | 覆盖该 PR 原有的 `-merged` 报告，不影响其他报告 |
| 历史 `force_merged` 报告 | 旧流程遗留，只保留、不再产生 |

合入报告与 check 报告是两个文件，文件名只差 `-merged` 后缀；看板只展示 `display=true` 的合入报告。

> 注意：没有 `test.sh` 的应用，check 报告是 `CI_passed=false`，但合入报告按上述规则强制置为 `true`，会出现“已通过但 `test_cases` 为空”的记录。该类应用是否需要展示，需要维护者确认（见第 9 节）。

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
| P2 | 合入时未找到 check 报告（未生成合入报告），或历史 `force_merged` | 已合入 | CI 维护者补跑 check 后重新触发 push；无法补跑则人工补录并复核 | 合入后 24 小时 |

## 7. 上游沟通与关闭

- 提 issue / PR 时必须带上第 5 节的最小证据，并注明镜像版本与硬件；
- 上游回复或修复后，把链接回填到 `unsupported_reasons` 的对应条目（schema 预留了 `{code, message, evidence, checked_at}` 结构；当前实现是纯文本，证据先放在 issue 或 PR 评论里）；
- 关闭条件：上游修复并在新版本验证通过，或上游明确声明不支持；两种情况都要更新对应报告。

## 8. 复核与数据生命周期

- 没有索引目录：check 阶段直接写 `openeuler-docker-images-test/<首字母>/<应用>/<版本>/<hardware>-<os>-<UTC>.json`，并写入 `prid`；
- 每个 `<应用>/<版本>/` 目录最多保留 3 个报告：超过时按时间删除最旧的**非 merged** 报告；`-merged` 报告（以及历史 `PR_merged=true` 的报告）不会被删除，且计入这 3 个名额；
- 同一 PR 合入后重跑会覆盖该 PR 原有的合入报告；其他运行按时间戳各自保留；
- 只有 `display=true` 的报告出现在看板；`aborted`、`error`、测试失败、未合入的报告一律不上看板。

## 9. 已知限制（当前实现）

| 限制 | 影响 | 处理方式 |
| --- | --- | --- |
| 服务型镜像被 `--entrypoint bash` 覆盖入口，服务不会启动 | kvrocks、flannel 这类脚本依赖 `docker exec` 或默认入口，测试必然失败；flannel 的 `docker: command not found` 文本还可能被误判为“通过” | 需要改为先启动容器再执行用例，或把脚本改成镜像内自检 |
| `PASS:` / `FAIL:` 与 `test_xxx()` 按定义顺序对齐 | 脚本里的辅助函数也打印 `PASS:` 时用例名错位（如 hertz 出现 `check_3`…`check_9`） | 修 `test.sh`，让每条用例只输出一行结果，辅助函数不输出结果行 |
| 无测试脚本时未写 `skip_reason=no_test_script` | 报告中无法直接区分“无脚本”和“测试失败”；合入后还会变成“通过但零用例” | 后续在报告模型中补充该字段，并明确是否允许展示 |
| `unsupported_reasons` 与 schema 未对齐 | CI 写纯文本，schema 要求 `{code, message}` 对象，报告无法通过 schema 校验 | 二选一：实现改为对象，或调整 schema 允许字符串 |
| 历史 `PR_merged=true` 的报告也按 merged 保护 | 这类文件不会进入保留策略清理，与“只有 `-merged` 才永久保留”的表述不一致 | 需要明确历史数据策略：保留并计入名额，或一次性清理 |
| 源码修改识别存在漏报 | 树内写入、删除后重新生成、格式化工具、脚本内间接 patch 都不会命中 | 见第 3.2 节，遇到时人工判断；后续按需补充规则 |

## 10. 记录示例

check 报告（`r/redis/8.10.3/kunpeng920-oe2403sp4-20261102T090411Z.json`）：

```json
{
  "schema_version": "0.5",
  "software": "redis",
  "version": "8.10.3",
  "hardware": "kunpeng920",
  "os": "oe2403sp4",
  "test_time": "20261102T090411Z",
  "finished_at": "20261102T090913Z",
  "run_status": "completed",
  "display": false,
  "CI_passed": false,
  "PR_merged": false,
  "upstream_supported": false,
  "unsupported_reasons": [
    "功能测试未通过",
    "Dockerfile 修改了上游源码: line 19: sed -ri 's!^( *createBoolConfig...' /redis/src/config.c;"
  ],
  "test_cases": [],
  "prid": "5483"
}
```

合入后生成的报告（`k/kitex/0.14.5/kunpeng920-oe2403sp4-20261102T121328Z-merged.json`）：

```json
{
  "schema_version": "0.5",
  "software": "kitex",
  "version": "0.14.5",
  "hardware": "kunpeng920",
  "os": "oe2403sp4",
  "test_time": "20261102T121328Z",
  "finished_at": "20261102T121328Z",
  "run_status": "completed",
  "display": true,
  "CI_passed": true,
  "PR_merged": true,
  "upstream_supported": true,
  "unsupported_reasons": [],
  "test_cases": [
    {
      "name": "test_version",
      "description": "exact version: v0.14.1",
      "category": "functional",
      "result": "pass",
      "test_time": "20261102T115249Z"
    },
    {
      "name": "test_code_generation",
      "description": "thrift code generation produced kitex_gen service code",
      "category": "functional",
      "result": "pass",
      "test_time": "20261102T115249Z"
    }
  ],
  "prid": "5490"
}
```

## 11. 责任分工

| 角色 | 职责 |
| --- | --- |
| PR 作者 | 修复 Dockerfile / `test.sh`，提供复现信息，确认是否修改了上游源码 |
| CI 维护者 | 维护判定规则、解析逻辑与报告仓库；处理缺失的合入报告；推动已知限制修复 |
| 上游维护者 / 社区 | 确认或修复上游缺陷；对“不支持 aarch64”给出明确结论 |
| 看板 / 数据使用方 | 只消费 `display=true` 的报告；`false` 仅用于问题统计与复核 |
