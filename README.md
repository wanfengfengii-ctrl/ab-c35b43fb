# Maintenance Window Expander

卫星地面站按**所在地民用时间**配置周期维护窗，本服务把它们确定地展开成 UTC 区间，
在夏令时切换处既不漏停也不重复停机。

## 快速开始

```bash
# 启动应用（宿主机端口可通过 APP_HOST_PORT 配置，默认 8080）
docker compose up --build app
APP_HOST_PORT=9090 docker compose up --build app

# 一次性验证：清洁启动后执行 单元测试 -> 构建检查 -> 覆盖 DST 边界的 HTTP 冒烟，
# 并以退出码汇报（0 = 全部通过）
docker compose up --build --exit-code-from verify
docker compose down
```

健康检查：`GET /health` → `{"status": "ok"}`（Dockerfile 与 Compose 均配置了探针）。

## API

### `POST /api/maintenance-windows/expand`

```json
{
  "timezone": "America/New_York",            // IANA 时区
  "rangeStartUtc": "2026-03-01T00:00:00Z",   // UTC 半开查询范围 [start, end)
  "rangeEndUtc":   "2026-04-01T00:00:00Z",
  "ambiguousTimePolicy": "earlier",          // 歧义时刻策略: earlier | later
  "rules": [                                 // 1..20 条，id 唯一
    {
      "id": "daily-check",
      "startLocal": "2026-03-08T02:30:00",   // 本地墙上时间，不带时区
      "durationMinutes": 30,                 // 正整数
      "frequency": "DAILY",                  // DAILY | WEEKLY
      "interval": 1,                         // 1..30
      "weekdays": ["MO", "WE"],              // 仅 WEEKLY：互异、非空
      "count": 3                             // count 或 until（本地、含端点）二选一
    }
  ]
}
```

成功响应（按 UTC 起点、再按规则编号稳定排序）：

```json
{
  "total": 1,
  "windows": [
    {
      "ruleId": "daily-check",
      "localStart": "2026-03-08T02:30:00",
      "utcStart": "2026-03-08T07:30:00Z",
      "utcEnd": "2026-03-08T08:00:00Z",
      "utcOffset": "-04:00",
      "resolution": "gap-shifted"
    }
  ]
}
```

每项返回**原本地发生时间**（`localStart`）与**所采用的 UTC 偏移**（`utcOffset`）。

## 展开语义

- **周期沿本地日历推进**：DAILY 每 `interval` 天、WEEKLY 每 `interval` 周（周一为一周起点，
  首周跳过早于 `startLocal` 的星期），墙上时刻不变，UTC 偏移随夏令时自动变化。
- **不存在的墙上时刻**（春季拨快）：向前移动**恰好时区跳变长度**（如 02:30 → 03:30，
  `resolution = "gap-shifted"`）。
- **重复时刻**（秋季拨回）：按 `ambiguousTimePolicy` 取 UTC 上更早（`earlier`）或更晚
  （`later`）的那个实例。
- **结束点** = 解析后的 UTC 起点 + `durationMinutes`（不随墙上时间伸缩）。
- **查询范围只筛选起点**：`rangeStartUtc <= utcStart < rangeEndUtc`，结束点越界不影响。
- **展开上限**：所有规则合计生成超过 **10000** 项即整体拒绝。

## 错误

错误响应形如 `{"error": {"code", "message", "ruleId?"}}`，失败信息可定位到规则编号：

| 情形 | HTTP | code |
| --- | --- | --- |
| 未知 IANA 时区 | 400 | `UNKNOWN_TIMEZONE` |
| 展开超过 10000 项 | 400 | `EXPANSION_LIMIT_EXCEEDED` |
| 终止条件缺失或矛盾（count/until 须恰居其一）、非法本地时间、非法规则字段 | 422 | `VALIDATION_ERROR` |

## 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q                                   # 单元 + API 测试
uvicorn app.main:app --port 8080            # 本地运行
APP_BASE_URL=http://127.0.0.1:8080 python verify.py   # 对本地实例跑 verify
```
