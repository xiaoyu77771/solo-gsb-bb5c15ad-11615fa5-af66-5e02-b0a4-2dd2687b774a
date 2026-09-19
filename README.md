# Firmware Image Assembly Service

FastAPI 服务：接收 Intel HEX 文本，逐行校验（字节数 + 二补校验和），按地址组装固件映像，
返回合并后的连续 Base64 段、段间空洞、入口地址与逐行诊断；并可针对一组命名内存区域
做布局校验（覆盖率、data/entry 权限、区域占用率）。

## 快速启动（单条命令）

```bash
docker compose up --build
```

启动后访问：

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

不使用 Compose 时：

```bash
docker build -t hexasm . && docker run --rm -p 8000:8000 hexasm
```

本地开发（无 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 接口

### `POST /api/v1/assemble`

JSON 请求体：

```json
{
  "hex_text": ":020000040800F2\n:10000000214601360121470136007EFE09D2190141\n:04000005080001C12D\n:00000001FF\n"
}
```

### `POST /api/v1/assemble/raw`

`text/plain` 请求体，直接粘贴 Intel HEX 文本：

```bash
curl -s -X POST http://localhost:8000/api/v1/assemble/raw \
  -H 'Content-Type: text/plain' \
  --data-binary @firmware.hex | jq
```

### 响应

无论 HEX 是否通过校验均返回 200，结果以 `ok` 与 `diagnostics` 判定：

```json
{
  "ok": true,
  "error_count": 0,
  "warning_count": 0,
  "entry_address": 134218177,
  "total_bytes": 16,
  "segment_count": 1,
  "segments": [
    {"start": 134217728, "end": 134217744, "size": 16, "data_base64": "IUYBNgEhRwE2AH7+CdIZAQ=="}
  ],
  "gaps": [],
  "diagnostics": [
    {"line": 1, "status": "ok", "record_type": "extended_linear_address",
     "address": 134217728, "byte_count": null, "message": "linear base set to 0x8000000"},
    {"line": 2, "status": "ok", "record_type": "data",
     "address": 134217728, "byte_count": 16, "message": "data: 16 byte(s) at 0x8000000"}
  ]
}
```

- `segments[]`：按地址升序合并的连续段，`data_base64` 为段内容，`end` 为开区间。
- `gaps[]`：相邻段之间的空洞（`start` 含、`end` 不含）。
- `entry_address`：最后一个 03/05 型记录给出的入口地址，无则为 `null`。
- `diagnostics[]`：逐行结果，`line` 从 1 开始，`0` 表示文件级问题（如缺失 EOF）。

### `POST /api/v1/validate-layout`

在组装结果之上做内存布局校验。请求体在 `hex_text` 之外增加 `regions`，
每个区域为半开区间 `[start, end)`：

```bash
curl -s -X POST http://localhost:8000/api/v1/validate-layout \
  -H 'Content-Type: application/json' \
  -d '{
    "hex_text": ":020000040800F2\n:10000000214601360121470136007EFE09D2190141\n:04000005080001C12D\n:00000001FF\n",
    "regions": [
      {"name": "flash",  "start": 134217728, "end": 134479872, "allow_data": true, "allow_entry": true},
      {"name": "config", "start": 134479872, "end": 134484992, "allow_data": true, "allow_entry": false}
    ]
  }' | jq
```

响应示例（同样恒为 200）：

```json
{
  "ok": true,
  "hex_ok": true,
  "regions_valid": true,
  "error_count": 0,
  "warning_count": 0,
  "entry_address": 134218177,
  "total_bytes": 16,
  "regions": [
    {
      "name": "flash", "start": 134217728, "end": 134479872, "size": 262144,
      "allow_data": true, "allow_entry": true,
      "occupied_bytes": 16, "utilization": 0.00006103515625, "violations": []
    },
    {
      "name": "config", "start": 134479872, "end": 134484992, "size": 5120,
      "allow_data": true, "allow_entry": false,
      "occupied_bytes": 0, "utilization": 0.0, "violations": []
    }
  ],
  "violations": [],
  "diagnostics": [ ... ]
}
```

判定规则：

- HEX 无效时只返回 `diagnostics`（`regions`/`violations` 为空、`regions_valid` 为 `null`），
  不执行布局判断。
- HEX 有效时先检查区域定义：每个区域须满足 `start < end`，且所有区域互不重叠
  （相邻边界相接允许）；定义不合法时 `regions_valid=false`，不再做字节级判定。
- 定义合法后，将固件连续段按区域边界切分，逐字节归属区域，并报告：
  - `uncovered_data`：未被任何区域覆盖的字节范围（`region` 为 `null`）；
  - `data_not_allowed`：落入 `allow_data=false` 区域的字节范围；
  - `entry_uncovered` / `entry_not_allowed`：入口地址（存在时）未落入任何区域，
    或落入 `allow_entry=false` 的区域。
- 每个区域汇总 `occupied_bytes`（落入的固件字节数）、`utilization`
  （`occupied_bytes / size`）及该区域关联的 `violations`；顶层 `violations`
  汇总全部违规。`ok` 仅在 HEX 有效且无任何违规时为 `true`。

### `POST /api/v1/plan-relocation`

固件整体重定位规划：对**所有固件段和入口地址施加同一个整数 `delta`**，
段内容与段间距离保持不变。在 `hex_text`、`regions` 之外可传 `max_abs_shift`
限制 `|delta|`。同样复用 HEX 解析与区域定义校验，任一无效即只返回诊断、
不做规划。

```bash
curl -s -X POST http://localhost:8000/api/v1/plan-relocation \
  -H 'Content-Type: application/json' \
  -d '{
    "hex_text": ":020000040800F2\n:10000000214601360121470136007EFE09D2190141\n:04000005080001C12D\n:00000001FF\n",
    "regions": [
      {"name": "flash", "start": 134283264, "end": 135266304, "allow_data": true, "allow_entry": true}
    ],
    "max_abs_shift": 1048576
  }' | jq
```

可行判定（全部为闭区间整数）：

- 每个固件段必须**完整落入**某个 `allow_data=true` 的区域（不允许跨区域边界）；
- 入口地址（存在时）必须落入某个 `allow_entry=true` 的区域；
- 搬移后所有地址必须仍为 32 位无符号地址（`0`..`0xFFFFFFFF`）；
- 给定 `max_abs_shift` 时还须满足 `|delta| <= max_abs_shift`。

响应在上述约束下求交集并合并出全部可行 delta 区间 `feasible_ranges`，
并选择 **`|delta|` 最小、绝对值相同再取数值较小（偏向负方向）** 的那个：

```json
{
  "ok": true,
  "hex_ok": true,
  "regions_valid": true,
  "error_count": 0,
  "warning_count": 0,
  "entry_address": 134218177,
  "total_bytes": 16,
  "max_abs_shift": 1048576,
  "feasible": true,
  "delta": 65536,
  "abs_shift": 65536,
  "feasible_ranges": [
    {"min_delta": 65536, "max_delta": 1048126}
  ],
  "relocated_entry_address": 134283713,
  "relocated_segments": [
    {"start": 134283264, "end": 134283280, "size": 16,
     "data_base64": "IUYBNgEhRwE2AH7+CdIZAQ=="}
  ],
  "violations": [],
  "diagnostics": [ ... ]
}
```

- `feasible`：是否存在满足全部约束的 delta；为 `false` 时
  `feasible_ranges` 为空，`delta`/`abs_shift` 为 `null`，
  且不返回 `relocated_segments`/`relocated_entry_address`。
- `feasible_ranges[]`：合并后的互不相交闭区间，`min_delta`/`max_delta`
  均含端点。
- `delta`：选中的统一搬移量；`relocated_segments[].data_base64` 为段内容，
  与 `/api/v1/assemble` 相同（搬移只改地址，不改字节）。
- HEX 无效（`hex_ok=false`、`regions_valid=null`）或区域定义不合法
  （`regions_valid=false`，`violations` 给出 `invalid_region`/
  `region_overlap`）时均不规划，`feasible=false`。
- 省略 `max_abs_shift` 时仅受区域与 32 位地址空间约束；对没有任何段与入口
  的空映像，可行集无界，返回 `delta=0` 与空的 `feasible_ranges`。

## 解析规则

- 每行必须形如 `:llaaaatt<data>cc`；校验 `ll`（字节数）与实际数据长度一致，
  且全部字节（含 `cc`）之和 ≡ 0 (mod 256)（二补校验和）。
- 支持的记录类型：
  - `00` 数据：写入 `当前基址 + 记录内偏移`；
  - `01` EOF：必须唯一且位于末行，其后出现任何记录（含第二个 EOF）均报错，缺失 EOF 同样报错；
  - `02` 扩展段地址：基址 = 值 << 4；
  - `03` 段入口（CS:IP）：入口 = CS << 4 + IP；
  - `04` 扩展线性地址：基址 = 值 << 16；
  - `05` 线性入口：入口 = 32 位值。
- 同一地址重复写入相同字节：接受（诊断中注明 identical rewrite）；
  写入冲突字节：报错并给出当前行号、冲突地址与首次写入的行号，保留首次写入的值。
- 校验失败的记录不会写入映像；空行忽略并记 warning。

## 项目结构

```
app/
  main.py        # FastAPI 应用与请求/响应模型
  hex_parser.py  # Intel HEX 解析与映像组装（纯逻辑，可独立测试）
  layout.py      # 内存布局校验（区域定义检查、段切分、占用统计，纯逻辑）
  relocation.py  # 整体重定位规划（可行 delta 闭区间求交合并、选最小位移，纯逻辑）
tests/
  test_hex_service.py       # 解析器与 assemble 接口
  test_validate_layout.py   # validate-layout 接口与布局逻辑
  test_plan_relocation.py   # plan-relocation 接口与重定位逻辑
Dockerfile
docker-compose.yml
requirements.txt       # 运行依赖
requirements-dev.txt   # 测试依赖
```
