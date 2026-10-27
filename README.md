# 鲲鹏测试结果文件（Kunpeng Test Result Files）

存放鲲鹏（Kunpeng 920 / 950）上 openEuler 容器镜像的测试结果。


## 目录：直接复用 openeuler-docker-images

结果文件放在 [upstream_dashboard](https://github.com/opensourceways/upstream-dashboard) 的对应分支，KunpengTest：

```text
openeuler-docker-images-test/
└── <应用分类>/                      # AI / Bigdata / Storage / Database / Cloud / HPC / Distroless / Others
    └── <应用名>/                    # 例如 mysql、redis、nginx、pytorch
        └── <应用版本>/              # 例如 9.7.0
            └── <openEuler 版本>/    # 例如 24.03-lts-sp4
                └── <hardware>-<YYYYMMDDTHHMMSSZ>.json
```

示例：

```text
Database/mysql/9.7.0/24.03-lts-sp4/kunpeng920-20261023T170000Z.json
Database/redis/8.10.2/24.03-lts-sp4/kunpeng950-20261024T120000Z.json
Cloud/nginx/1.31.8/24.03-lts-sp4/kunpeng950-20261025T060000Z.json
AI/pytorch/2.14.1/24.03-lts-sp4/kunpeng920-20261022T080000Z.json
```

约定：

- 目录已经表达 `应用名 / 应用版本 / openEuler 版本`；JSON 里保留 `software`、`version` 字段，方便单文件自解释和校验；`openEuler 版本` 以目录为准。
- 一个文件 = 一次测试运行；**不覆盖历史**。重测写新文件即可。
- 文件名 = `<hardware>-<UTC 时间>.json`；同一秒内多次运行时追加 `-<6位run_id>`。
- 没有 `index.json`，也没有读写接口；看板或脚本直接遍历目录读取。

## 字段速查

完整定义见 [schema/test-result.schema.json](schema/test-result.schema.json)。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema_version` | 是 | 固定 `"1.0"` |
| `software` / `version` | 是 | 应用名 / 应用版本，需与目录一致 |
| `hardware` | 是 | `kunpeng920` / `kunpeng950`，需与文件名一致 |
| `test_time` | 是 | 运行开始时间（UTC），需与文件名里的时间一致 |
| `finished_at` | 否 | 运行结束时间（UTC） |
| `run_status` | 否 | `completed`（默认）/ `aborted` / `error` |
| `upstream_unsupported` | 是 | 上游官方是否不支持鲲鹏 / aarch64 |
| `unsupported_reasons` | 是 | 原因列表；`upstream_unsupported=false` 时必须为 `[]` |
| `test_cases` | 是 | 用例列表，允许为空（`aborted/error` 时） |

用例结果 `result` 取值：`pass` / `fail` / `skip` / `blocked` / `error`。

## 新增一次测试结果

1. 在对应镜像目录下找到 `<应用版本>/<openEuler 版本>/`。
2. 新建 `<hardware>-<YYYYMMDDTHHMMSSZ>.json`，按 schema 填写。
3. 运行校验：

```powershell
cd D:\kunpeng-test-result-api
python .\scripts\validate_examples.py
```

脚本会检查：必填字段与枚举、目录名与字段是否一致、文件名与 `hardware/test_time` 是否一致、`unsupported_reasons` 一致性。

## 示例场景

| 场景 | 示例文件 |
| --- | --- |
| 全通过（kunpeng920） | [kunpeng920-20261025T020000Z.json](examples/results/Cloud/nginx/1.31.8/24.03-lts-sp4/kunpeng920-20261025T020000Z.json) |
| 同版本跨硬件（kunpeng950） | [kunpeng950-20261025T060000Z.json](examples/results/Cloud/nginx/1.31.8/24.03-lts-sp4/kunpeng950-20261025T060000Z.json) |
| 部分失败 + 性能不达标 | [kunpeng950-20261024T120000Z.json](examples/results/Database/redis/8.10.2/24.03-lts-sp4/kunpeng950-20261024T120000Z.json) |
| 重测恢复 | [kunpeng950-20261026T040000Z.json](examples/results/Database/redis/8.10.2/24.03-lts-sp4/kunpeng950-20261026T040000Z.json) |
| 运行中止、空用例 | [kunpeng920-20261023T170000Z.json](examples/results/Database/mysql/9.7.0/24.03-lts-sp4/kunpeng920-20261023T170000Z.json) |
| 上游不支持但本地测试通过 | [kunpeng920-20261022T080000Z.json](examples/results/AI/pytorch/2.14.1/24.03-lts-sp4/kunpeng920-20261022T080000Z.json) |
