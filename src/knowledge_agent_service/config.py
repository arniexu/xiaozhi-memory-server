from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.sqlite3"

    @property
    def vector_db(self) -> Path:
        return self.data_dir / "vectors.sqlite3"

    @property
    def event_log(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_uri and self.neo4j_user and self.neo4j_password)


def load_settings() -> Settings:
    data_dir = Path(
        os.getenv("KNOWLEDGE_AGENT_DATA_DIR", "~/.local/share/knowledge-agent-service")
    ).expanduser().resolve()
    return Settings(
        data_dir=data_dir,
        host=os.getenv("KNOWLEDGE_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("KNOWLEDGE_AGENT_PORT", "8765")),
        neo4j_uri=os.getenv("KNOWLEDGE_NEO4J_URI", ""),
        neo4j_user=os.getenv("KNOWLEDGE_NEO4J_USER", ""),
        neo4j_password=os.getenv("KNOWLEDGE_NEO4J_PASSWORD", ""),
        neo4j_database=os.getenv("KNOWLEDGE_NEO4J_DATABASE", "neo4j"),
    )