"""本地演示数据：为 /docs 演示准备两个批次的旧卡与对照表。

用法（数据库位置由 EXHIBITION_DB 决定，默认 /data/exhibition.db）：

    python -m app.demo_data                      # 本地运行
    docker compose exec api python -m app.demo_data   # 容器内

可重复执行：会把演示批次重置到初始状态（v1 对照表内容 + 七张卡）。
若现行对照表内容已不同（例如演示过换表），重置会产生一个新版本号，
这是换表接口的正常语义，不影响演示流程。

演示故事线（配合 README「旧卡检索演示」一节）：
  - 批次 M1987：1987 年旧编目批次，对照表 v1 有缺码（0A06/0A07 未收录），
    且 0A02 与 0A08 都映射为「花」——卡 0006 与 0007 因此同名「明青花」。
  - 批次 B2001：另一批，对照表独立，用来验证换表互不影响。
"""
from __future__ import annotations

from .db import connect, init_db, write_transaction
from .legacy import record_card, swap_mapping_table

BATCH_A = "M1987"
BATCH_B = "B2001"

# 对照表 v1：0A06/0A07 缺映射；0A02 与 0A08 同文（旧系统异体重复码）。
TABLE_A_V1 = {
    "0B01": "明",
    "0B02": "清",
    "0A01": "青",
    "0A02": "花",
    "0A03": "瓷",
    "0A04": "瓶",
    "0A05": "碗",
    "0A08": "花",
}

CARDS_A = {
    "0001": ["0B01", "0A01", "0A02", "0A03", "0A04"],  # 明青花瓷瓶
    "0002": ["0B01", "0A01", "0A02", "0A03", "0A05"],  # 明青花瓷碗
    "0003": ["0B02", "0A01", "0A02", "0A03", "0A04"],  # 清青花瓷瓶
    "0004": ["0B02", "0A01", "0A02", "0A03", "0A06"],  # 0A06 缺映射 → 不完整
    "0005": ["0B01", "0A07", "0A04"],                  # 0A07 缺映射 → 不完整
    "0006": ["0B01", "0A01", "0A02"],                  # 明青花
    "0007": ["0B01", "0A01", "0A08"],                  # 明青花（与 0006 同名）
}

TABLE_B = {"1001": "近", "1002": "代", "1003": "油", "1004": "画"}
CARDS_B = {"0001": ["1001", "1002", "1003", "1004"]}  # 近代油画


def seed() -> None:
    init_db()
    conn = connect()
    try:
        with write_transaction(conn):
            table_a = swap_mapping_table(conn, BATCH_A, TABLE_A_V1)
            for card_no, codes in CARDS_A.items():
                record_card(conn, BATCH_A, card_no, codes)
            table_b = swap_mapping_table(conn, BATCH_B, TABLE_B)
            for card_no, codes in CARDS_B.items():
                record_card(conn, BATCH_B, card_no, codes)
    finally:
        conn.close()
    print(f"批次 {BATCH_A}: 对照表 v{table_a.version}（{table_a.status}），卡片 {len(CARDS_A)} 张")
    print(f"批次 {BATCH_B}: 对照表 v{table_b.version}（{table_b.status}），卡片 {len(CARDS_B)} 张")
    print("接下来可在 /docs 试：")
    print(f"  GET /legacy/batches/{BATCH_A}/cards/0004        # 看未映射位置 [4]")
    print("  GET /legacy/cards/by-name?name=明青花           # 同名卡 0006 + 0007")
    print(f"  PUT /legacy/batches/{BATCH_A}/mapping-table     # 换 v2 表（README 有样例）")


if __name__ == "__main__":
    seed()
