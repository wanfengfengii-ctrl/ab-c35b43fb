# Maintenance Window Expander

卫星地面站按地面站所在地的民用本地时间配置周期性维护窗。本服务把这些本地
周期规则**确定地**展开为 UTC 半开区间，正确处理夏令时（DST）跳变：春季
不存在的墙上时刻向前移动恰好跳变长度，秋季重复时刻按策略取较早或较晚的
瞬间，保证维护既不漏停也不重复停机。

## 快速开始

```bash
# 宿主机端口可配置（默认 8080）
APP_PORT=9090 docker compose up --build

curl http://localhost:9090/health
```

一次性验证服务（清洁构建后跑测试 + 构建 + DST 边界 HTTP 冒烟，以退出码
汇报）：

```bash
docker compose build
docker compose run --rm verify
echo $?   # 0 表示全部通过
```

`verify` 服务等待 `app` 健康检查通过后才运行，因此冒烟打的是真实 HTTP
栈。

## API

`POST /api/maintenance-windows/expand`

```json
{
  "timeZone": "America/New_York",
  "rangeStartUtc": "2024-01-01T00:00:00Z",
  "rangeEndUtc": "2024-03-01T00:00:00Z",
  "ambiguousTime": "earlier",
  "rules": [
    {
      "id": 1,
      "startLocal": "2024-01-01T02:30:00",
      "durationMinutes": 90,
      "frequency": "DAILY",
      "interval": 1,
      "count": 10
    },
    {
      "id": 2,
      "startLocal": "2024-03-04T03:00:00",
      "durationMinutes": 60,
      "frequency": "WEEKLY",
      "interval": 2,
      "untilLocal": "2024-12-31T03:00:00",
      "byWeekday": [1, 5]
    }
  ]
}
```

| 字段 | 约束 |
| --- | --- |
| `timeZone` | IANA 时区名（如 `Asia/Shanghai`、`Australia/Lord_Howe`） |
| `rangeStartUtc` / `rangeEndUtc` | 带时区的 UTC 查询范围，半开 `[start, end)` |
| `ambiguousTime` | `earlier`（fold=0，第一次出现）或 `later`（fold=1） |
| `rules` | 1–20 条，`id` 唯一 |
| `startLocal` | 无时区后缀的本地墙上时间 |
| `durationMinutes` | 正整数 |
| `frequency` | `DAILY` / `WEEKLY` |
| `interval` | 1–30（每 N 天 / 每 N 周） |
| 终止条件 | `count`（正整数）与 `untilLocal`（本地时间，含当天）**二选一** |
| `byWeekday` | WEEKLY 必填，互异整数，1=周一 … 7=周日；`startLocal` 的星期必须在集合内 |

成功响应按 `(startUtc, ruleId)` 稳定排序，每条返回所采用的 UTC 偏移与
原本地发生时间：

```json
{
  "timeZone": "America/New_York",
  "rangeStartUtc": "2024-01-01T00:00:00Z",
  "rangeEndUtc": "2025-01-01T00:00:00Z",
  "ambiguousTime": "earlier",
  "count": 1,
  "windows": [
    {
      "ruleId": 1,
      "occurrence": 3,
      "frequency": "DAILY",
      "durationMinutes": 90,
      "startLocal": "2024-03-10T02:30:00",
      "startUtc": "2024-03-10T07:30:00Z",
      "endUtc": "2024-03-10T09:00:00Z",
      "utcOffset": "-04:00",
      "resolution": "GAP_FORWARD_SHIFTED"
    }
  ]
}
```

`resolution` 取值：`UNAMBIGUOUS`、`GAP_FORWARD_SHIFTED`（不存在时刻已按
跳变长度前移）、`AMBIGUOUS_EARLIER`、`AMBIGUOUS_LATER`。

## DST 语义

- **周期沿本地日历推进**：加 N 个本地日 / 周，不做 UTC 等距平移。
- **春季跳变（间隙）**：如纽约 2024-03-10 `02:30` 不存在（时钟
  `02:00 → 03:00`），解析到的瞬间恰好是把墙上时钟前移 1 小时后的
  `03:30 EDT = 07:30Z`；支持 30 分钟级跳变（如 Lord Howe 岛）。
- **秋季回退（歧义）**：如纽约 2024-11-03 `01:30` 出现两次，`earlier`
  → `05:30Z (-04:00)`，`later` → `06:30Z (-05:00)`，由请求策略确定性
  选择，不会产生重复停机。
- **结束点** = 解析后的 UTC 起点 + `durationMinutes`（纯 UTC 运算，
  跨 DST 边界时持续时长精确不变）。
- **查询范围只筛起点**，半开区间；区间跨出范围终点的窗不会因终点而
  纳入。
- 迭代终止同时使用本地墙上时间上界与 fold=0/fold=1 双判定，保证
  回退日边界处不漏判。

## 拒绝条件

所有语义错误返回 HTTP 422（结构错误 422，FastAPI 解析错误）并附带可定位
规则编号的错误体：

```json
{"error": {"code": "UNBOUNDED_RULE", "message": "rule 7: ...", "ruleId": 7}}
```

涵盖：未知时区、无界（count/until 均缺）、矛盾终止（两者都给、until 早于
start）、非法本地时间字符串、本地时间带了时区后缀、查询范围非法/无时区、
歧义策略非法、规则 id 重复、WEEKLY 星期集合缺失/重复/越界、startLocal
星期不在集合内、DAILY 误带 byWeekday，以及整个请求展开超过 10,000 项
（`EXPANSION_LIMIT_EXCEEDED`，在范围过滤**之前**按闭式计数判定，避免
借小查询区间绕过）。

## 本地开发

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
./scripts/verify.sh          # 测试 + 本地起服 + HTTP 冒烟
```
