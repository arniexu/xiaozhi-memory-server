"""盘点/清理：今晚 xiaozhi 测试导入同步进 Neo4j 的合成实体。

用法（用知识服务自己的 venv 跑）：
    只读盘点： python /tmp/cleanup_test_entities.py
    执行清理： python /tmp/cleanup_test_entities.py --apply
"""
import os
import sqlite3
import sys

sys.path.insert(0, "/home/xuqj/knowledge-agent-service/src")

env_path = os.path.expanduser("~/.config/knowledge-agent-service/environment")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]  # systemd 会剥引号，这里手动对齐语义
                os.environ[key] = value

from knowledge_agent_service.config import load_settings  # noqa: E402
from knowledge_agent_service.graph_store import Neo4jGraphStore  # noqa: E402

APPLY = "--apply" in sys.argv
TEST_WORKSPACES = ("xiaozhi-livetest", "xiaozhi-battery")

settings = load_settings()
connection = sqlite3.connect(settings.memory_db)
rows = connection.execute(
    "SELECT id, workspace_id FROM memory_units WHERE workspace_id IN (?, ?)",
    TEST_WORKSPACES,
).fetchall()
node_ids = [row[0] for row in rows if str(row[0]).startswith("knowledge:")]
print(f"SQLite 命中测试单元: {len(rows)} 条（其中图谱节点候选 {len(node_ids)}）")

store = Neo4jGraphStore(settings)
with store._driver() as driver, driver.session(database=settings.neo4j_database) as session:
    found = session.run(
        "MATCH (n:KnowledgeEntity) WHERE n.id IN $ids RETURN count(n) AS c",
        ids=node_ids,
    ).single()["c"]
    edge_found = session.run(
        "MATCH (a:KnowledgeEntity)-[r]-() WHERE a.id IN $ids RETURN count(r) AS c",
        ids=node_ids,
    ).single()["c"]
    print(f"Neo4j 命中节点: {found} ｜ 关联边: {edge_found}")

    if APPLY:
        session.run(
            "MATCH (n:KnowledgeEntity) WHERE n.id IN $ids DETACH DELETE n",
            ids=node_ids,
        )
        remaining = session.run(
            "MATCH (n:KnowledgeEntity) WHERE n.id IN $ids RETURN count(n) AS c",
            ids=node_ids,
        ).single()["c"]
        print(f"清理完成：剩余命中节点 {remaining}（关联边随 DETACH 删除）")
    else:
        print("（只读盘点；加 --apply 执行清理）")
