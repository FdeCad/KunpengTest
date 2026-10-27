# 上游不支持规则码表（示例）

## 1. 语义

`upstream_supported` 表示**上游官方是否明确支持鲲鹏 / Linux aarch64 / 目标 OS**，含义不是“本次测试是否通过”。

- 上游没有 aarch64 官方包，但发行版仓库有可用的 aarch64 构建：`upstream_supported=false`，测试仍可执行，结果可以是 `pass`。
- 上游支持 aarch64，但本次用例失败：`upstream_supported=true`，失败归因于产品、环境或用例，不得写成“上游不支持”。
- 上游已 EOL / 明确声明放弃该架构：`upstream_supported=false`。
- 仅仅“我们没有在鲲鹏上验证过”不构成上游不支持。

## 2. 原因对象

```json
{
  "code": "NO_UPSTREAM_CUDA_AARCH64",
  "message": "上游 CUDA 版本不提供 Linux aarch64 官方支持，本次只测试 CPU 版本。",
  "evidence": [
    "https://pytorch.org/get-started/locally/",
    "https://docs.nvidia.com/cuda/cuda-installation-guide-linux/"
  ],
  "checked_at": "2026-10-22T07:30:00Z"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `code` | 是 | 稳定规则码，大写蛇形，发布后不复用、不改语义 |
| `message` | 是 | 面向看板的中文说明，由规则模板生成，避免手写自由文本 |
| `evidence` | 否 | 判定证据链接，建议至少 1 条 |
| `checked_at` | 否 | 规则判定时间，UTC |

多个规则同时命中时，`unsupported_reasons` 放多条，按 `code` 字典序排序。

## 3. 规则码

| code | 判定含义 | 典型证据 | `upstream_supported` |
| --- | --- | --- | --- |
| `NO_UPSTREAM_AARCH64_BINARY` | 上游 release 只提供 x86_64 二进制/安装包，无 aarch64 产物 | 上游下载页、release assets 列表 | `false` |
| `NO_UPSTREAM_AARCH64_WHEEL` | 上游包仓库没有 Linux aarch64 wheel/包 | PyPI、npm、Maven Central 等文件列表 | `false` |
| `NO_UPSTREAM_AARCH64_IMAGE` | 上游容器镜像没有 linux/arm64 manifest | registry manifest、官方 Dockerfile | `false` |
| `NO_UPSTREAM_OS_SUPPORT` | 上游明确只支持特定 OS，目标 OS 不在支持列表 | 官方支持矩阵、安装文档 | `false` |
| `NO_UPSTREAM_CUDA_AARCH64` | 上游 GPU/CUDA 版本不提供 Linux aarch64 支持 | 官方安装矩阵 | `false` |
| `UPSTREAM_EOL` | 上游版本已 EOL，不再提供修复或构建 | 官方 EOL 公告、生命周期页面 | `false` |
| `UPSTREAM_DEPRECATED` | 上游已标记 deprecated，建议迁移 | 官方 deprecation notice | `false` |
| `UPSTREAM_ARCH_DROPPED` | 上游曾支持 aarch64，后续版本明确移除 | release notes、issue、commit | `false` |
| `LICENSE_RESTRICTED` | 许可证限制导致无法合法分发/验证 | 许可证文本、官方声明 | `false` |
| `NO_UPSTREAM_RELEASE` | 上游不存在该版本或该版本未正式发布 | release 页面、tag 列表 | `false` |

新增规则时：先在表里登记 `code` 和语义，再让规则引擎产出；统计时只按 `code` 聚合，不解析 `message`。

## 4. 一致性约束

- `upstream_supported=false` → `unsupported_reasons` 至少 1 条。
- `upstream_supported=true` → `unsupported_reasons` 必须为 `[]`。
- 不允许用 `upstream_supported=false` 掩盖测试环境问题；环境问题应写 `run_status=error/aborted` 或用例 `result=error`。
- `message` 可以随证据更新，但 `code` 不变；统计以 `code` 为准。

## 5. 建议的规则执行顺序

1. 版本是否存在（`NO_UPSTREAM_RELEASE`）。
2. 架构/OS 是否在官方支持矩阵（`NO_UPSTREAM_AARCH64_*`、`NO_UPSTREAM_OS_SUPPORT`）。
3. 生命周期状态（`UPSTREAM_EOL`、`UPSTREAM_DEPRECATED`、`UPSTREAM_ARCH_DROPPED`）。
4. 许可证/分发限制（`LICENSE_RESTRICTED`）。
5. 输出全部命中的规则，而不是只取第一条；可用 `code` 统计 Top 原因。
