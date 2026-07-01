"""
任务队列 Redis 双写工具。

提交端调用 push_* 把 "id:asin" 推入对应 Redis 队列。
single 评论任务用 Redis ZSET，score=priority + need_crawler_time 组合分；另用 Hash 保存 priority。
worker 不再用 need_crawler_time 做执行门槛，而是在队列候选中优先取 priority 最小的任务；
priority 相同再按 need_crawler_time 最早、id 最小消费。
temp/asin 任务继续用 Redis List，worker 通过 pop_task() BLPOP 取任务后用 id claim MySQL 行。
Backfill 用 list_queue_members() 扫现有队列对账。

Key 设计（最简版）：
  crawler:queue:amazon:review:single:US        ← 90% 流量
  crawler:queue:amazon:review:single:other     ← 非 US 评论
  crawler:queue:amazon:review:temp             ← temp 任务（不分 region）
  crawler:queue:amazon:asin                    ← ASIN 详情（不分 region）

这些任务队列统一放在 Redis DB1；WorkerRecovery 的 active-worker key 也放 DB1，
便于和任务队列一起排查。REDIS_DB 继续留给 stop_signal、daemon 心跳、静态 IP 池等状态。

约束：
- Redis 不可达时 push 静默失败（不影响 MySQL 落库），backfill 负责补齐。
- pop 返回 None 表示队列空或 Redis 不可达；worker 等待下一轮，不再扫 MySQL pending 表。
- 客户端进程内 lazy 缓存，避免重复连接。
"""
import os
import time
from datetime import date, datetime
from typing import Iterable, Optional, Tuple

import redis as _redis_lib
from redis.exceptions import ResponseError
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.config import (
    REDIS_HOST, REDIS_PASSWORD, REDIS_PORT, REDIS_QUEUE_DB, REDIS_USERNAME,
)

KEY_REVIEW_SINGLE_US    = "crawler:queue:amazon:review:single:US"
KEY_REVIEW_SINGLE_OTHER = "crawler:queue:amazon:review:single:other"
KEY_REVIEW_TEMP         = "crawler:queue:amazon:review:temp"
KEY_ASIN                = "crawler:queue:amazon:asin"

# 同步给 worker 和 backfill 用，避免散落
QUEUE_KEYS = {
    "single_us":    KEY_REVIEW_SINGLE_US,
    "single_other": KEY_REVIEW_SINGLE_OTHER,
    "temp":         KEY_REVIEW_TEMP,
    "asin":         KEY_ASIN,
}

_client: Optional[_redis_lib.Redis] = None
_single_zset_ready = set()

SINGLE_QUEUE_KEYS = {KEY_REVIEW_SINGLE_US, KEY_REVIEW_SINGLE_OTHER}
_POP_POLL_SECONDS = 1.0


def _get_client() -> Optional[_redis_lib.Redis]:
    global _client
    if _client is not None:
        return _client
    try:
        _client = _redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_QUEUE_DB, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=15,
        )
    except Exception as e:
        logger.warning(f"[TaskQueue] Redis 初始化失败，task_id 仅存 MySQL: {e}")
        _client = None
    return _client


def _reset_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None


def _region_group(region: str) -> str:
    return "US" if (region or "").upper() == "US" else "other"


def review_single_key_for_region(region: str) -> str:
    """根据 region 字符串返回对应的 single review 队列 key。"""
    return KEY_REVIEW_SINGLE_US if _region_group(region) == "US" else KEY_REVIEW_SINGLE_OTHER


def is_single_queue_key(key: str) -> bool:
    """single 评论队列使用 ZSET，其它队列仍使用 List。"""
    return str(key or "") in SINGLE_QUEUE_KEYS


DEFAULT_TASK_PRIORITY = 100
SINGLE_PRIORITY_HASH_SUFFIX = ":priority"
SINGLE_SCORE_PRIORITY_FACTOR = 1_000_000_000
SINGLE_SCORE_EPOCH = datetime(2020, 1, 1).timestamp()
try:
    SINGLE_DUE_SCAN_LIMIT = max(1, int(os.getenv("SINGLE_DUE_SCAN_LIMIT", "1000")))
except (TypeError, ValueError):
    SINGLE_DUE_SCAN_LIMIT = 1000


def normalize_task_priority(priority=None, default: int = DEFAULT_TASK_PRIORITY) -> int:
    """Normalize task priority. Smaller numbers are consumed first."""
    if priority is None or str(priority).strip() == "":
        return int(default)
    raw = str(priority).strip().lower()
    label_map = {
        "p0": 0,
        "p1": 100,
        "p2": 200,
        "explore": 900,
    }
    if raw in label_map:
        return label_map[raw]
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(default)


def _need_time_timestamp(need_crawler_time=None) -> float:
    if need_crawler_time is None or str(need_crawler_time).strip() == "":
        return time.time()
    if isinstance(need_crawler_time, (int, float)):
        return float(need_crawler_time)
    if isinstance(need_crawler_time, datetime):
        return need_crawler_time.timestamp()
    if isinstance(need_crawler_time, date):
        return datetime.combine(need_crawler_time, datetime.min.time()).timestamp()

    raw = str(need_crawler_time).strip()
    candidates = (
        (raw[:19], "%Y-%m-%d %H:%M:%S"),
        (raw[:19], "%Y-%m-%dT%H:%M:%S"),
        (raw[:10], "%Y-%m-%d"),
    )
    for value, fmt in candidates:
        try:
            return datetime.strptime(value, fmt).timestamp()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        logger.warning(f"[TaskQueue] need_crawler_time={raw!r} 无法解析，使用当前时间入队")
        return time.time()


def make_priority_score(need_crawler_time=None, priority=None) -> float:
    """
    Redis ZSET score 采用 priority 主导的组合分：
      priority * SINGLE_SCORE_PRIORITY_FACTOR + need_crawler_time 相对偏移。

    这样 Redis 自身排序就是 priority 越小越靠前；同 priority 下，
    need_crawler_time 越早越靠前。priority 仍写旁路 hash，用于兼容旧队列分数。
    """
    priority_val = normalize_task_priority(priority)
    time_offset = max(0.0, _need_time_timestamp(need_crawler_time) - SINGLE_SCORE_EPOCH)
    return float(priority_val * SINGLE_SCORE_PRIORITY_FACTOR + time_offset)


def _single_priority_hash_key(key: str) -> str:
    return f"{key}{SINGLE_PRIORITY_HASH_SUFFIX}"


def _redis_key_type(r, key: str) -> str:
    try:
        value = r.type(key)
        return value.decode() if isinstance(value, bytes) else str(value)
    except Exception:
        return "unknown"


def _ensure_single_zset(r, key: str) -> bool:
    """
    single 队列从 List 升级到 ZSET。
    如果发现旧 List key，直接删除；任务仍在 MySQL，backfill 会按 priority + need_crawler_time 重建队列。
    """
    if not is_single_queue_key(key):
        return False
    if key in _single_zset_ready:
        return True
    key_type = _redis_key_type(r, key)
    if key_type in ("none", "zset"):
        _single_zset_ready.add(key)
        return True
    if key_type == "list":
        try:
            depth = r.llen(key)
            r.delete(key)
            _single_zset_ready.add(key)
            logger.warning(
                f"[TaskQueue] 已清理旧 List single 队列 key={key} depth={depth}，"
                "等待 backfill 按 priority + need_crawler_time 重建 ZSET"
            )
            return True
        except Exception as e:
            logger.warning(f"[TaskQueue] 清理旧 single List 队列失败 key={key}: {e}")
            return False
    logger.warning(f"[TaskQueue] single 队列 key={key} 类型异常 type={key_type}，跳过")
    return False


def _zpop_one(r, key: str) -> Optional[str]:
    """原子取出一个 single ZSET 任务；priority 越小越优先，同 priority 按时间/id 排序。"""
    script = """
    local function member_id(item)
        return tonumber(string.match(item, '^(%d+)') or item) or 0
    end
    local function time_score(score, priority)
        local factor = tonumber(ARGV[3])
        local epoch = tonumber(ARGV[4])
        local base = priority * factor
        if score >= base then
            local offset = score - base
            if offset >= 0 and offset < factor then
                return offset
            end
        end
        if score > epoch then
            return score - epoch
        end
        return score
    end
    local rows = redis.call('ZRANGE', KEYS[1], 0, tonumber(ARGV[1]) - 1, 'WITHSCORES')
    if #rows == 0 then
        return nil
    end
    local best_item = nil
    local best_score = nil
    local best_time_score = nil
    local best_priority = nil
    local best_id = nil
    for i = 1, #rows, 2 do
        local item = rows[i]
        local score = tonumber(rows[i + 1])
        local priority = tonumber(redis.call('HGET', KEYS[2], item) or ARGV[2])
        local item_time_score = time_score(score, priority)
        local item_id = member_id(item)
        if best_item == nil
            or priority < best_priority
            or (priority == best_priority and item_time_score < best_time_score)
            or (priority == best_priority and item_time_score == best_time_score and item_id < best_id)
        then
            best_item = item
            best_score = score
            best_time_score = item_time_score
            best_priority = priority
            best_id = item_id
        end
    end
    if best_item ~= nil and redis.call('ZREM', KEYS[1], best_item) == 1 then
        redis.call('HDEL', KEYS[2], best_item)
        return best_item
    end
    return nil
    """
    item = r.eval(
        script,
        2,
        key,
        _single_priority_hash_key(key),
        SINGLE_DUE_SCAN_LIMIT,
        DEFAULT_TASK_PRIORITY,
        SINGLE_SCORE_PRIORITY_FACTOR,
        SINGLE_SCORE_EPOCH,
    )
    return str(item) if item is not None else None


def _zpop_from_keys(r, keys: Iterable[str]) -> Optional[Tuple[str, str]]:
    """从多个 single ZSET 中原子取 priority 最小的任务；同 priority 按时间/id 排序。"""
    queue_keys = [str(k) for k in keys if k]
    if not queue_keys:
        return None
    script = """
    local function member_id(item)
        return tonumber(string.match(item, '^(%d+)') or item) or 0
    end
    local function time_score(score, priority)
        local factor = tonumber(ARGV[3])
        local epoch = tonumber(ARGV[4])
        local base = priority * factor
        if score >= base then
            local offset = score - base
            if offset >= 0 and offset < factor then
                return offset
            end
        end
        if score > epoch then
            return score - epoch
        end
        return score
    end
    local key_count = tonumber(ARGV[5])
    local best_key = nil
    local best_hash_key = nil
    local best_item = nil
    local best_score = nil
    local best_time_score = nil
    local best_priority = nil
    local best_id = nil
    for i = 1, key_count do
        local key = KEYS[i]
        local hash_key = KEYS[i + key_count]
        local rows = redis.call('ZRANGE', key, 0, tonumber(ARGV[1]) - 1, 'WITHSCORES')
        if #rows > 0 then
            for j = 1, #rows, 2 do
                local item = rows[j]
                local score = tonumber(rows[j + 1])
                local priority = tonumber(redis.call('HGET', hash_key, item) or ARGV[2])
                local item_time_score = time_score(score, priority)
                local item_id = member_id(item)
                if best_item == nil
                    or priority < best_priority
                    or (priority == best_priority and item_time_score < best_time_score)
                    or (priority == best_priority and item_time_score == best_time_score and item_id < best_id)
                then
                    best_key = key
                    best_hash_key = hash_key
                    best_item = item
                    best_score = score
                    best_time_score = item_time_score
                    best_priority = priority
                    best_id = item_id
                end
            end
        end
    end
    if best_key ~= nil and redis.call('ZREM', best_key, best_item) == 1 then
        redis.call('HDEL', best_hash_key, best_item)
        return {best_key, best_item}
    end
    return nil
    """
    priority_keys = [_single_priority_hash_key(key) for key in queue_keys]
    result = r.eval(
        script,
        len(queue_keys) * 2,
        *queue_keys,
        *priority_keys,
        SINGLE_DUE_SCAN_LIMIT,
        DEFAULT_TASK_PRIORITY,
        SINGLE_SCORE_PRIORITY_FACTOR,
        SINGLE_SCORE_EPOCH,
        len(queue_keys),
    )
    if not result:
        return None
    return str(result[0]), str(result[1])


def _sleep_until_next_poll(r, keys: Iterable[str], deadline: float) -> None:
    remaining = deadline - time.time()
    if remaining <= 0:
        return
    next_score = None
    for key in keys:
        try:
            rows = r.zrange(key, 0, 0, withscores=True)
            if rows:
                score = float(rows[0][1])
                next_score = score if next_score is None else min(next_score, score)
        except Exception:
            pass
    if next_score is None:
        sleep_seconds = min(_POP_POLL_SECONDS, remaining)
    else:
        sleep_seconds = min(max(0.05, next_score - time.time()), _POP_POLL_SECONDS, remaining)
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


# ─────────────────────────────────────────────────────────────────────────
#  生产端：push
# ─────────────────────────────────────────────────────────────────────────

def make_queue_payload(row_id, asin: str = "") -> str:
    """Redis 队列值统一为 '<mysql_id>:<asin>'，便于按主键 claim 和人工排查。"""
    if row_id is None:
        return ""
    row_id = str(row_id).strip()
    if not row_id:
        return ""
    asin = str(asin or "").strip().upper()
    return f"{row_id}:{asin}" if asin else row_id


def parse_queue_payload(payload) -> Tuple[str, str]:
    """
    解析 Redis 队列值。
    兼容旧格式：纯 task_id / 纯 id；新格式：id:asin。
    """
    raw = str(payload or "").strip()
    if ":" not in raw:
        return raw, ""
    row_id, asin = raw.split(":", 1)
    return row_id.strip(), asin.strip().upper()


def queue_payload_identities(payload) -> set:
    """返回可用于去重的标识集合：完整 payload + 冒号前 id。"""
    raw = str(payload or "").strip()
    row_id, _ = parse_queue_payload(raw)
    return {item for item in (raw, row_id) if item}


def push_single_task(row_id, asin: str, region: str, need_crawler_time=None, priority=None) -> bool:
    """提交 single 评论任务后调用。region 决定队列，priority 决定消费顺序。"""
    r = _get_client()
    if r is None:
        return False
    key = review_single_key_for_region(region)
    payload = make_queue_payload(row_id, asin)
    if not payload:
        return False
    try:
        if not _ensure_single_zset(r, key):
            return False
        r.zadd(key, {payload: make_priority_score(need_crawler_time, priority=priority)})
        r.hset(_single_priority_hash_key(key), payload, normalize_task_priority(priority))
        return True
    except ResponseError:
        _single_zset_ready.discard(key)
        try:
            if not _ensure_single_zset(r, key):
                return False
            r.zadd(key, {payload: make_priority_score(need_crawler_time, priority=priority)})
            r.hset(_single_priority_hash_key(key), payload, normalize_task_priority(priority))
            return True
        except Exception as e:
            logger.warning(f"[TaskQueue] push_single_task 重试失败 payload={payload}: {e}")
            return False
    except Exception as e:
        logger.warning(f"[TaskQueue] push_single_task 失败 payload={payload}: {e}")
        return False


def push_temp_tasks(tasks: Iterable) -> int:
    """批量推送 temp 任务。元素可为 id、(id, asin) 或 {'id': ..., 'asin': ...}。"""
    ids = []
    for item in tasks:
        if item is None:
            continue
        if isinstance(item, dict):
            payload = make_queue_payload(item.get("id"), item.get("asin", ""))
        elif isinstance(item, (tuple, list)) and item:
            asin = item[1] if len(item) > 1 else ""
            payload = make_queue_payload(item[0], asin)
        else:
            payload = make_queue_payload(item)
        if payload:
            ids.append(payload)
    if not ids:
        return 0
    r = _get_client()
    if r is None:
        return 0
    try:
        r.rpush(KEY_REVIEW_TEMP, *ids)
        return len(ids)
    except Exception as e:
        logger.warning(f"[TaskQueue] push_temp_tasks 失败 count={len(ids)}: {e}")
        return 0


def push_asin_task(row_id, asin: str = "") -> bool:
    r = _get_client()
    if r is None:
        return False
    payload = make_queue_payload(row_id, asin)
    if not payload:
        return False
    try:
        r.rpush(KEY_ASIN, payload)
        return True
    except Exception as e:
        logger.warning(f"[TaskQueue] push_asin_task 失败 payload={payload}: {e}")
        return False


def push_to_key(key: str, task_ids: Iterable) -> int:
    """通用 push（backfill 补回缺失任务时用）。"""
    ids = [str(i) for i in task_ids if i is not None]
    if not ids:
        return 0
    r = _get_client()
    if r is None:
        return 0
    try:
        if is_single_queue_key(key):
            if not _ensure_single_zset(r, key):
                return 0
            score = make_priority_score()
            r.zadd(key, {payload: score for payload in ids})
            r.hset(_single_priority_hash_key(key), mapping={payload: DEFAULT_TASK_PRIORITY for payload in ids})
            return len(ids)
        r.rpush(key, *ids)
        return len(ids)
    except Exception as e:
        logger.warning(f"[TaskQueue] push_to_key 失败 key={key} count={len(ids)}: {e}")
        return 0


def push_single_to_key(key: str, tasks: Iterable[Tuple[str, object]]) -> int:
    """
    backfill 专用：把 single payload 按 priority、need_crawler_time 写入 ZSET。
    tasks 元素格式为 (payload, need_crawler_time) 或 (payload, need_crawler_time, priority)。
    """
    mapping = {}
    priority_mapping = {}
    for item in tasks:
        if not item:
            continue
        payload = item[0]
        need_crawler_time = item[1] if len(item) > 1 else None
        priority = item[2] if len(item) > 2 else None
        payload = str(payload or "").strip()
        if payload:
            mapping[payload] = make_priority_score(need_crawler_time, priority=priority)
            priority_mapping[payload] = normalize_task_priority(priority)
    if not mapping:
        return 0
    r = _get_client()
    if r is None:
        return 0
    try:
        if not _ensure_single_zset(r, key):
            return 0
        r.zadd(key, mapping)
        if priority_mapping:
            r.hset(_single_priority_hash_key(key), mapping=priority_mapping)
        return len(mapping)
    except Exception as e:
        logger.warning(f"[TaskQueue] push_single_to_key 失败 key={key} count={len(mapping)}: {e}")
        return 0


def missing_single_payloads(key: str, candidates: Iterable[Tuple[str, Iterable[str], object]]) -> list:
    """
    single backfill 专用：只检查候选任务是否已在 ZSET，避免全量 ZRANGE 大队列。
    candidates 元素格式为 (payload, identities, need_crawler_time) 或
    (payload, identities, need_crawler_time, priority)。
    返回缺失的 [(payload, need_crawler_time, priority), ...]。
    """
    items = []
    for item in candidates:
        if len(item) < 3:
            continue
        payload, identities, need_crawler_time = item[:3]
        priority = item[3] if len(item) > 3 else None
        payload = str(payload or "").strip()
        if not payload:
            continue
        identity_set = {payload}
        identity_set.update(str(item).strip() for item in (identities or []) if str(item or "").strip())
        items.append((payload, sorted(identity_set), need_crawler_time, priority))
    if not items:
        return []

    r = _get_client()
    if r is None:
        return []
    try:
        if not _ensure_single_zset(r, key):
            return []
        pipe = r.pipeline()
        spans = []
        offset = 0
        for _, identities, _, _ in items:
            spans.append((offset, offset + len(identities)))
            offset += len(identities)
            for identity in identities:
                pipe.zscore(key, identity)
        results = pipe.execute()
        missing = []
        for idx, (payload, _, need_crawler_time, priority) in enumerate(items):
            start, end = spans[idx]
            if not any(score is not None for score in results[start:end]):
                missing.append((payload, need_crawler_time, priority))
        return missing
    except Exception as e:
        logger.warning(f"[TaskQueue] missing_single_payloads 失败 key={key} count={len(items)}: {e}")
        return []


def remove_single_payloads(key: str, payloads: Iterable[str]) -> int:
    """从 single ZSET 删除一批 payload。Backfill 超时清理 stale Redis 记录时用。"""
    members = [str(item or "").strip() for item in payloads if str(item or "").strip()]
    if not members:
        return 0
    r = _get_client()
    if r is None:
        return 0
    try:
        if not is_single_queue_key(key):
            return 0
        if not _ensure_single_zset(r, key):
            return 0
        removed = int(r.zrem(key, *members) or 0)
        r.hdel(_single_priority_hash_key(key), *members)
        return removed
    except Exception as e:
        logger.warning(f"[TaskQueue] remove_single_payloads 失败 key={key} count={len(members)}: {e}")
        return 0


# ─────────────────────────────────────────────────────────────────────────
#  消费端：pop
# ─────────────────────────────────────────────────────────────────────────

def pop_task(key: str, timeout_seconds: int = 10) -> Optional[str]:
    """
    BLPOP 取一个队列 payload。
    - 超时（队列空）返回 None
    - Redis 不可达返回 None（worker 等待下一轮）
    """
    r = _get_client()
    if r is None:
        return None
    try:
        result = r.blpop(key, timeout=timeout_seconds)
        if result is None:
            return None
        _, task_id = result
        return str(task_id)
    except Exception as e:
        logger.warning(f"[TaskQueue] pop_task 失败 key={key}: {e}")
        return None


def pop_task_from_keys(keys: Iterable[str], timeout_seconds: int = 10) -> Optional[Tuple[str, str]]:
    """
    从多个 Redis List 中 BLPOP 一个 payload，返回 (key, payload)。
    single 队列已改用 pop_single_task_from_keys()。
    """
    queue_keys = [str(k) for k in keys if k]
    if not queue_keys:
        return None
    r = _get_client()
    if r is None:
        return None
    try:
        result = r.blpop(queue_keys, timeout=timeout_seconds)
        if result is None:
            return None
        key, task_id = result
        return str(key), str(task_id)
    except Exception as e:
        logger.warning(f"[TaskQueue] pop_task_from_keys 失败 keys={queue_keys}: {e}")
        return None


def pop_single_task(key: str, timeout_seconds: int = 10) -> Optional[str]:
    """
    从 single ZSET 取一个任务。
    priority 越小越优先；priority 相同按 need_crawler_time、id 消费。
    """
    r = _get_client()
    if r is None:
        return None
    deadline = time.time() + max(0, int(timeout_seconds or 0))
    try:
        if not _ensure_single_zset(r, key):
            return None
        while True:
            payload = _zpop_one(r, key)
            if payload:
                return payload
            if time.time() >= deadline:
                return None
            _sleep_until_next_poll(r, [key], deadline)
    except ResponseError:
        _single_zset_ready.discard(key)
        logger.warning(f"[TaskQueue] pop_single_task 发现 key 类型冲突 key={key}，等待下轮重试")
        return None
    except Exception as e:
        logger.warning(f"[TaskQueue] pop_single_task 失败 key={key}: {e}")
        return None


def pop_single_task_from_keys(keys: Iterable[str], timeout_seconds: int = 10) -> Optional[Tuple[str, str]]:
    """
    从多个 single ZSET 中取 priority 最小的任务，返回 (key, payload)。
    用于未指定国家的 single worker 同时消费 US / other 队列。
    """
    queue_keys = [str(k) for k in keys if k]
    if not queue_keys:
        return None
    r = _get_client()
    if r is None:
        return None
    deadline = time.time() + max(0, int(timeout_seconds or 0))
    try:
        for key in queue_keys:
            if not _ensure_single_zset(r, key):
                return None
        while True:
            result = _zpop_from_keys(r, queue_keys)
            if result:
                return result
            if time.time() >= deadline:
                return None
            _sleep_until_next_poll(r, queue_keys, deadline)
    except ResponseError:
        for key in queue_keys:
            _single_zset_ready.discard(key)
        logger.warning(f"[TaskQueue] pop_single_task_from_keys 发现 key 类型冲突 keys={queue_keys}，等待下轮重试")
        return None
    except Exception as e:
        logger.warning(f"[TaskQueue] pop_single_task_from_keys 失败 keys={queue_keys}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
#  Backfill / 监控
# ─────────────────────────────────────────────────────────────────────────

def list_queue_members(key: str) -> set:
    """返回队列内所有 payload（字符串 set）。Backfill 用来对账。"""
    r = _get_client()
    if r is None:
        return set()
    try:
        if is_single_queue_key(key):
            if not _ensure_single_zset(r, key):
                return set()
            return set(r.zrange(key, 0, -1))
        return set(r.lrange(key, 0, -1))
    except Exception as e:
        logger.warning(f"[TaskQueue] list_queue_members 失败 key={key}: {e}")
        return set()


def queue_length(key: str) -> int:
    """查询某队列当前长度（监控用）。Redis 不可达时返回 -1。"""
    r = _get_client()
    if r is None:
        return -1
    try:
        if is_single_queue_key(key):
            if not _ensure_single_zset(r, key):
                return -1
            return int(r.zcard(key))
        return int(r.llen(key))
    except Exception as e:
        logger.warning(f"[TaskQueue] queue_length 失败 key={key}: {e}")
        _reset_client()
        return -1


def queue_due_length(key: str) -> int:
    """查询当前可消费的队列长度。single ZSET 不再按 need_crawler_time 过滤。"""
    r = _get_client()
    if r is None:
        return -1
    try:
        if is_single_queue_key(key):
            if not _ensure_single_zset(r, key):
                return -1
            return int(r.zcard(key))
        return int(r.llen(key))
    except Exception as e:
        logger.warning(f"[TaskQueue] queue_due_length 失败 key={key}: {e}")
        _reset_client()
        return -1


def queue_lengths_snapshot() -> dict:
    """一次性返回所有队列长度，方便 dashboard / 报警。"""
    return {name: queue_length(key) for name, key in QUEUE_KEYS.items()}


def queue_due_lengths_snapshot() -> dict:
    """一次性返回所有队列当前可消费长度。"""
    return {name: queue_due_length(key) for name, key in QUEUE_KEYS.items()}
