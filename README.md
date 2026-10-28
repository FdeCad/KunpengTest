# 鲲鹏测试结果文件（Kunpeng Test Result Files）

存放鲲鹏（Kunpeng 920 / 950）上 openEuler / Ubuntu 容器镜像的测试结果。


## 目录：按应用名首字母 A-Z 分片

结果文件放在 [upstream_dashboard](https://github.com/opensourceways/upstream-dashboard) 的对应分支，KunpengTest：

```text
openeuler-docker-images-test/
└── <A-Z>/                           # 应用名首字母大写，例如 F、M、P、X
    └── <应用名>/                    # 例如 faiss、mysql、pytorch、x264
        └── <应用版本>/              # 例如 1.8.0、26.7.0、28_19
            └── <hardware>-<os>-<YYYYMMDDTHHMMSSZ>.json
```

示例：

```text
F/faiss/1.8.0/kunpeng920-oe2403sp4-20261022T130819Z.json    # os 信息在 JSON 里
M/mysql/26.7.0/kunpeng950-oe2403sp4-20261023T070920Z.json
X/x264/0480cb05/kunpeng950-oe2403sp4-20261023T080301Z.json
O/openjdk/28_19/kunpeng920-oe2403sp4-20261022T155634Z.json
```

约定：

- 首字母分片目录 = 应用名首字母大写（非字母开头归入 `0-9`），便于按前缀查找软件。
- OS 信息记录在 JSON 的 `os` 字段，并体现在文件名中；不占用目录层级。取简写，如 `oe2403sp4`（openEuler 24.03 LTS SP4）、`ub2604`（Ubuntu 26.04.x）。
- 目录已经表达 `应用名 / 应用版本`；JSON 里保留 `software`、`version` 字段，方便单文件自解释和校验。
- 一个文件 = 一次测试运行；**不覆盖历史**。重测写新文件即可。
- 文件名 = `<hardware>-<OS 简写>-<UTC 时间>.json`，例如 `kunpeng920-oe2403sp4-20261022T130819Z.json`；同一秒内多次运行时追加 `-<6位run_id>`。
- 没有 `index.json`，也没有读写接口；看板或脚本直接遍历目录读取。

## 字段速查

完整定义见 [schema/test-result.schema.json](schema/test-result.schema.json)。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `"4.0"` |
| `software` / `version` | 是 | 应用名 / 应用版本，需与目录一致 |
| `hardware` | 是 | `kunpeng920` / `kunpeng950`，需与文件名一致 |
| `os` | 是 | 被测 OS 简写，如 `oe2403sp4`、`ub2604` |
| `test_time` | 是 | 运行开始时间（UTC），需与文件名里的时间一致 |
| `finished_at` | 否 | 运行结束时间（UTC） |
| `run_status` | 否 | `completed`（默认）/ `aborted` / `error` |
| `display` | 是 | 看板展示开关：仅当 `CI_passed` 与 `PR_merged` 都为 `true` 时才为 `true` |
| `CI_passed` | 是 | CI 测试是否通过：`true` 表示可在该硬件 + OS 上正常运行 |
| `PR_merged` | 是 | 对应 PR 是否已合入 |
| `upstream_supported` | 是 | 上游官方是否支持鲲鹏 / aarch64；`false` 时需在 `unsupported_reasons` 说明原因 |
| `unsupported_reasons` | 是 | 原因列表；`upstream_supported=false` 时至少 1 条，`true` 时必须为 `[]` |
| `test_cases` | 是 | 用例列表，允许为空（未执行到用例阶段，例如安装失败或 `aborted/error`） |

用例结果 `result` 取值：`pass` / `fail` / `skip` / `blocked` / `error`。

`display` 是看板唯一的展示开关：只有 `display=true` 的结果文件才会出现在看板上，且仅当 `CI_passed` 与 `PR_merged` 都为 `true` 时，`display` 才为 `true`。`CI_passed` 表示 CI 结论，为 `true` 时不要求所有用例都 `pass`（`skip` / `blocked` 可接受）；但运行中止（`aborted` / `error`）或存在 `fail` / `error` 用例时不应置为 `true`，校验脚本会对这类组合给出警告。`PR_merged` 记录对应 PR 的合入状态。

## 新增一次测试结果

1. 按应用名首字母进入 `<A-Z>/<应用名>/<应用版本>/`。
2. 新建 `<hardware>-<os>-<YYYYMMDDTHHMMSSZ>.json`，按 schema 填写。
3. 运行校验：

```powershell
cd D:\kunpeng-test-result-api
python .\scripts\validate_examples.py
```

脚本会检查：必填字段与枚举、`os` 格式、目录名与字段是否一致、文件名与 `hardware/os/test_time` 是否一致、`CI_passed` 与运行状态/用例结果是否矛盾、`upstream_supported` 与 `unsupported_reasons` 一致性。

## 示例场景

示例目录共 145 个结果文件，覆盖 45 个软件版本与鲲鹏 920 / 950：

- openEuler 24.03 LTS SP4（`oe2403sp4`）：90 个，全部通过；
- Ubuntu 26.04.1（`ub2604`）：55 个，含通过、失败与未完成（未开始的单元格不生成文件）。

| 场景 | 示例文件 |
| --- | --- |
| 同一版本双硬件 | [kunpeng920-oe2403sp4-20261022T130819Z.json](examples/results/F/faiss/1.8.0/kunpeng920-oe2403sp4-20261022T130819Z.json)、[kunpeng950-oe2403sp4-20261022T170731Z.json](examples/results/F/faiss/1.8.0/kunpeng950-oe2403sp4-20261022T170731Z.json) |
| 同一软件多版本 | [flink 1.16.3](examples/results/F/flink/1.16.3/kunpeng920-oe2403sp4-20261022T135634Z.json)、[flink 2.3.0](examples/results/F/flink/2.3.0/kunpeng950-oe2403sp4-20261023T062746Z.json) |
| 以 commit 作为版本号 | [x264 0480cb05](examples/results/X/x264/0480cb05/kunpeng950-oe2403sp4-20261023T080301Z.json) |
| 版本号含下划线 | [openjdk 28_19](examples/results/O/openjdk/28_19/kunpeng920-oe2403sp4-20261022T155634Z.json) |
| Ubuntu 通过 | [faiss 1.8.0 / kunpeng950](examples/results/F/faiss/1.8.0/kunpeng950-ub2604-20261022T171021Z.json) |
| Ubuntu 失败（未产出逐条用例结论） | [envoy 1.40.0 / kunpeng920](examples/results/E/envoy/1.40.0/kunpeng920-ub2604-20261021T150831Z.json) |
| Ubuntu 未完成（无最终结论） | [hnswlib 0.8.0 / kunpeng950](examples/results/H/hnswlib/0.8.0/kunpeng950-ub2604-20261023T015109Z.json) |
