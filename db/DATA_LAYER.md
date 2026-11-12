# 数据层设计（四层 + 只追加）

## 分层总览

```
来源系统                L1 原始事件层        L2 标准业务账本层                 L3 结算匹配层              L4 月度汇总层
CDC/Webhook/轮询/人工 → raw_events       → business_events（归一化）       settlement_batches       monthly_reports
                         （证据原样留存）     ledger_entries（双分录/三币种）  settlement_lines         monthly_report_versions
                                            fx_rates（历史汇率版本）         bank_transactions        （不可变快照，版本链）
                                            order/payment_states（投影）      settlement_matches
                                                                             discrepancies(+actions)
```

### L1 原始事件层 `raw_events`
- 唯一键 `(source, idempotency_key)`：CDC 用 LSN/消息 ID，webhook 用 event_id，轮询用游标+渠道参考号。
- 重放/重复推送在 L1 即被丢弃，原始报文 JSONB 原样留存。
- 仅 `status / processed_at / reject_reason` 生命周期列允许更新，证据列由触发器锁定。

### L2 标准业务账本层
- `business_events`：稳定业务键 `business_key` 全局唯一，多来源同一事实只入账一次；`event_version` + `supersedes_event_id` 表达修订链。
- `ledger_entries`：每个业务事件一组**借贷平衡**双分录；每行同时保留
  - 交易币 `txn_amount/txn_currency`、结算币 `settle_*`、记账币 `book_*`；
  - 两级汇率 `txn_to_settle_rate / settle_to_book_rate`、`rate_source`、`rate_effective_at`；
  - 精度 `decimal_places` 与 `raw_amount` 原始金额；
  - `effective_month` 业务归属期（更正账/补单可能晚于归属期过账）。
- `fx_rates`：只追加，更正 = 新版本（`is_correction`），历史事件永远按其发生时点汇率计量。
- `order_states / payment_states`：可更新的**投影**，按 `event_time` 重放；迟到旧事件不允许把状态打回。

### L3 结算匹配层
- 匹配两级：`ORDER_TO_SETTLEMENT`（订单事件↔结算行）、`SETTLEMENT_TO_BANK`（结算单↔银行流水，支持一笔流水合并多单、部分分配）。
- 差异五类：`AMOUNT / FEE / FX / TIMING / UNKNOWN`，均带 `dedup_key` 支持幂等重跑。
- 引擎只写 `PROPOSED` 匹配与 `OPEN` 差异；`UNKNOWN` 必须人工调账后才能关闭。
- `discrepancy_actions` 只追加，完整保留认领/附件/建议/过账/关闭轨迹。

### L4 月度汇总层
- `monthly_report_versions`：快照 JSONB 不可变；新版本产生时旧版本仅 `version_status` 置 `SUPERSEDED`（触发器放行该列），快照行本身禁改禁删。
- 快照内置 `conservation_check` 借贷守恒校验与指纹，内容无变化时复用当前版本。

## 只追加触发器（PostgreSQL）

`app/init_db.py` 为以下表安装 `BEFORE UPDATE OR DELETE` 与 `BEFORE TRUNCATE` 触发器：
`raw_events, business_events, fx_rates, ledger_entries, discrepancy_actions, monthly_report_versions`。

- 任何 UPDATE/DELETE/TRUNCATE 直接抛异常（`raw_events` 生命周期列、报表版本状态列例外，证据列仍不可改）。
- 被外键引用的表，TRUNCATE 还会先被外键约束拦截。
- 测试重置使用 `DROP SCHEMA public CASCADE`（DDL 级，不受行触发器限制）。

## 金额与汇率规则
- 统一 `Numeric(20,6)` 存储，展示按 `decimal_places`（默认 2）`ROUND_HALF_UP`。
- 分录平衡以记账币校验；结算币/交易币由记账币按事件时点汇率反推，三种币种下各自守恒。
- 汇率更正不回写历史分录，而是按受影响事件追加 `FX_GAIN_LOSS` 更正分录并产出新报表版本。
