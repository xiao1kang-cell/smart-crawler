from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class TwitterTaskRecord:
    """数据库中的 Twitter 任务行。"""
    id: int
    task_type: str
    input: str
    lang: str = ""
    retry_count: int = 0

    @classmethod
    def from_row(cls, row: Dict) -> "TwitterTaskRecord":
        return cls(
            id=int(row["id"]),
            task_type=row["task_type"],
            input=row["input"],
            lang=row.get("lang") or "",
            retry_count=int(row.get("retry_count") or 0),
        )


@dataclass
class TwitterTaskResult:
    """单次 Twitter 任务执行结果。"""
    tweets: List[Dict]
    pages_fetched: int = 0
