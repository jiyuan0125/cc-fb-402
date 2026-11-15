#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize_fin.py
把多币种账户流水 (txns.csv) 按阶梯汇率 (rates.json) 归一成人民币，写入 SQLite。

用法:
    python3 -I normalize_fin.py --txns <txns.csv> --rates <rates.json> --db <out.sqlite>

设计要点 (与库内 尾注表 内容一致):
1. 汇率档: 每币种一张 15 分钟阶梯表, 档上只给档起时刻与价格。一笔流水取
   "档起时刻 <= 交易时间" 的最后一档 (即该交易时间所在档, 含档起整点)。
2. 空档沿用 (forward-fill): 两档之间缺失的时段 (深夜整段空档) 沿用上一个
   实际存在的档; 价格使用表登记的是实际取价的那一档 (档起时刻早于交易时间),
   并以 档来源=空档沿用 标明。首档之前没有任何价格可沿用, 该笔挂起。
3. 冲销: 流水没有冲销标记, 按 (账户, 币种, 外币金额) 相同、方向相反且时间
   更晚配对 (本数据全部严格 1:1)。冲销行不按自身时间重新取价, 而是沿用被冲
   销那笔的同一档价, 使一对冲销折人民币后净额恰为 0; 价格使用表中冲销行与
   被冲销行登记同一档起时刻与价格, 以 档来源=冲销沿用 标明。
4. 未配价币种 (rates.json 中无表) 不硬换: 不入归一表, 登记到 挂起表。
5. 幂等: 每次重跑整库重建 (DROP + 按主键排序写入), 不依赖时钟与随机数,
   重跑后 归一表 / 价格使用表 / 日报 三处数字完全不变。
所有金额计算使用 Decimal, 人民币与汇率保留 6 位小数。
"""

import argparse
import csv
import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.000001")
TS_FMT = "%Y-%m-%d %H:%M:%S"
TIER_FMT = "%Y-%m-%d %H:%M"

SCHEMA = """
DROP TABLE IF EXISTS 归一表;
DROP TABLE IF EXISTS 价格使用表;
DROP TABLE IF EXISTS 日报;
DROP TABLE IF EXISTS 挂起表;
DROP TABLE IF EXISTS 尾注;

CREATE TABLE 归一表 (
    流水号      TEXT PRIMARY KEY,
    账户        TEXT NOT NULL,
    币种        TEXT NOT NULL,
    外币金额    TEXT NOT NULL,          -- 原样保留, Decimal 文本
    交易时间    TEXT NOT NULL,
    方向        TEXT NOT NULL,          -- 入 / 出
    成交汇率    TEXT NOT NULL,          -- 实际采用的档位价格
    人民币金额  TEXT NOT NULL,          -- 外币金额 * 成交汇率, 6 位小数, 恒为正
    人民币净流入 TEXT NOT NULL,         -- 入为正, 出为负
    档起时刻    TEXT NOT NULL,
    档来源      TEXT NOT NULL,          -- 当档直取 / 空档沿用 / 冲销沿用
    冲销对应流水号 TEXT                 -- 冲销行填被冲销流水号, 否则 NULL
);

CREATE TABLE 价格使用表 (
    流水号   TEXT PRIMARY KEY,
    币种     TEXT NOT NULL,
    档起时刻 TEXT NOT NULL,
    价格     TEXT NOT NULL,
    档来源   TEXT NOT NULL
);

CREATE TABLE 日报 (
    日期          TEXT PRIMARY KEY,     -- 按交易时间的自然日
    人民币净流入  TEXT NOT NULL,
    入笔数        INTEGER NOT NULL,
    出笔数        INTEGER NOT NULL,
    挂起笔数      INTEGER NOT NULL
);

CREATE TABLE 挂起表 (
    流水号   TEXT PRIMARY KEY,
    账户     TEXT NOT NULL,
    币种     TEXT NOT NULL,
    外币金额 TEXT NOT NULL,
    交易时间 TEXT NOT NULL,
    方向     TEXT NOT NULL,
    挂起原因 TEXT NOT NULL
);

CREATE TABLE 尾注 (
    编号  INTEGER PRIMARY KEY,
    条目  TEXT NOT NULL
);
"""

NOTES = [
    "取价规则: 每笔流水取『档起时刻 <= 交易时间』的最后一档 (交易时间所在 15 分钟档, 含档起时刻)。",
    "空档沿用: 阶梯表两档之间缺失的时段沿用上一个实际存在的档; 价格使用表登记实际取价档, 档来源标『空档沿用』。首档之前无价可沿用的流水挂起。",
    "冲销规则: 冲销无显式标记, 按同账户+同币种+同外币金额+反方向配对 (时间晚者为冲销行, 本数据 30 对严格 1:1)。冲销行沿用被冲销那笔的同一档价, 不按自身时间重新取价, 一对冲销折人民币净额恰为 0; 价格使用表登记同一档, 档来源标『冲销沿用』。",
    "未配价币种: rates.json 中无汇率表的币种不硬换, 不入归一表, 登记于挂起表 (本数据为 MXN、BRL)。",
    "金额口径: 人民币金额 = 外币金额 × 成交汇率, Decimal 计算, 四舍五入保留 6 位小数; 人民币净流入 入为正、出为负。日报按交易时间自然日汇总。",
    "幂等: 每次重跑整库重建, 按主键/日期排序写入, 不使用时钟与随机数; 归一表、价格使用表、日报三处数字重跑不变且互相咬合。",
]


def q6(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def load_rates(path):
    """返回 {币种: ([档起时刻 datetime 升序], [Decimal 价格])}。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    tables = {}
    for ccy, tiers in raw.items():
        pairs = sorted(
            ((datetime.strptime(t["档起时刻"], TIER_FMT), Decimal(str(t["价格"]))) for t in tiers),
            key=lambda p: p[0],
        )
        tables[ccy] = ([p[0] for p in pairs], [p[1] for p in pairs])
    return tables


def load_txns(path):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_amt"] = Decimal(r["外币金额"])
        r["_dt"] = datetime.strptime(r["交易时间"], TS_FMT)
    return rows


def pair_reversals(rows):
    """
    识别冲销对: (账户,币种,金额) 相同且方向相反的两笔, 晚者为冲销行。
    返回 {冲销流水号: 被冲销流水号}, 以及无法严格 1:1 配对的分组告警。
    """
    groups = defaultdict(list)
    for r in rows:
        groups[(r["账户"], r["币种"], r["外币金额"])].append(r)
    reversal_of = {}
    for key, grp in groups.items():
        if len(grp) == 1:
            continue
        if len(grp) != 2 or grp[0]["方向"] == grp[1]["方向"]:
            raise ValueError(f"冲销配对存在歧义, 分组 {key}: "
                             f"{[x['流水号'] for x in grp]}")
        earlier, later = sorted(grp, key=lambda x: x["_dt"])
        reversal_of[later["流水号"]] = earlier["流水号"]
    return reversal_of


def pick_tier(tables, ccy, dt):
    """
    返回 (档起时刻 datetime, 价格 Decimal, 档来源) 或 None。
    档来源: 当档直取 (命中的档起时刻与规则整点档一致), 否则 空档沿用。
    """
    if ccy not in tables:
        return None
    stamps, prices = tables[ccy]
    idx = bisect_right(stamps, dt) - 1
    if idx < 0:
        return None  # 首档之前, 无价可沿用
    stamp, price = stamps[idx], prices[idx]
    # 规则整点档: 交易时间向下取整到 15 分钟; 与实际档起一致说明该档存在
    floored = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    source = "当档直取" if floored == stamp else "空档沿用"
    return stamp, price, source


def main():
    ap = argparse.ArgumentParser(description="多币种流水归一人民币 -> SQLite")
    ap.add_argument("--txns", required=True)
    ap.add_argument("--rates", required=True)
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    tables = load_rates(args.rates)
    rows = load_txns(args.txns)
    reversal_of = pair_reversals(rows)

    # 预计算每笔按【自身时间】取到的档 (未配价/首档之前为 None), 不依赖处理顺序
    own_pick = {r["流水号"]: pick_tier(tables, r["币种"], r["_dt"]) for r in rows}

    normalized, price_used, suspended = [], [], []

    for r in sorted(rows, key=lambda x: x["流水号"]):
        tid, ccy = r["流水号"], r["币种"]
        amt, dt, direction = r["_amt"], r["_dt"], r["方向"]

        # 1) 未配价币种 -> 挂起
        if ccy not in tables:
            suspended.append((tid, r["账户"], ccy, r["外币金额"], r["交易时间"],
                              direction, f"币种 {ccy} 未配置汇率表, 不硬换"))
            continue

        # 2) 冲销行: 沿用被冲销那笔的档与价 (原笔按自身时间取价, 此处直接查预计算结果)
        if tid in reversal_of:
            orig_id = reversal_of[tid]
            pick = own_pick[orig_id]
            if pick is None:
                suspended.append((tid, r["账户"], ccy, r["外币金额"], r["交易时间"],
                                  direction, f"被冲销笔 {orig_id} 无价可沿用"))
                continue
            stamp, price, _ = pick
            source = "冲销沿用"
        else:
            pick = own_pick[tid]
            if pick is None:
                suspended.append((tid, r["账户"], ccy, r["外币金额"], r["交易时间"],
                                  direction, "交易时间早于该币种首个汇率档, 无价可沿用"))
                continue
            stamp, price, source = pick

        cny_abs = q6(amt * price)
        cny_net = cny_abs if direction == "入" else -cny_abs
        stamp_text = stamp.strftime(TIER_FMT)
        price_text = str(price)

        normalized.append((
            tid, r["账户"], ccy, r["外币金额"], r["交易时间"], direction,
            price_text, str(cny_abs), str(cny_net), stamp_text, source,
            reversal_of.get(tid),
        ))
        price_used.append((tid, ccy, stamp_text, price_text, source))

    # 3) 日报: 按交易时间自然日, 挂起笔只计数不进金额
    daily = defaultdict(lambda: [Decimal("0"), 0, 0, 0])  # 净流入, 入数, 出数, 挂起数
    for rec in normalized:
        day = rec[4][:10]
        daily[day][0] += Decimal(rec[8])
        daily[day][1 if rec[5] == "入" else 2] += 1
    for rec in suspended:
        daily[rec[4][:10]][3] += 1
    report = [(day, str(q6(v[0])), v[1], v[2], v[3]) for day, v in sorted(daily.items())]

    # 4) 写库 (整库重建保证幂等)
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO 归一表 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", normalized)
        conn.executemany(
            "INSERT INTO 价格使用表 VALUES (?,?,?,?,?)", price_used)
        conn.executemany(
            "INSERT INTO 日报 VALUES (?,?,?,?,?)", report)
        conn.executemany(
            "INSERT INTO 挂起表 VALUES (?,?,?,?,?,?,?)", suspended)
        conn.executemany(
            "INSERT INTO 尾注 (编号, 条目) VALUES (?, ?)",
            list(enumerate(NOTES, start=1)))
        conn.commit()
    finally:
        conn.close()

    # 5) 控制台摘要
    print(f"流水总数 {len(rows)} | 归一 {len(normalized)} | 挂起 {len(suspended)} "
          f"| 冲销对 {len(reversal_of)} | 日报天数 {len(report)}")


if __name__ == "__main__":
    main()
