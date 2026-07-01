"""
LongTermAnalyzer — 长期风险画像守护进程

职责：
  1. 每 6 小时从 account_daily_summary 读取近 7/30 天数据
  2. 5 维度风险评分：错误率趋势(30%) + 封号频率(25%) + 验证码频率(20%) + IP 多样性(10%) + 行为规律性(15%)
  3. 风险等级映射：low(0-25) / medium(26-50) / high(51-75) / critical(76-100)
  4. 将结果写入 account_risk_profile + Redis crawler:account:quota:{username}

在 get_reviews_main.py 的 __main__ 中启动：
    from app.crawlers.amazon_crawler.shuler.util.long_term_analyzer import LongTermAnalyzer
    analyzer = LongTermAnalyzer()
    analyzer.start()
"""
import math
import time
import traceback
from datetime import datetime, timedelta
from multiprocessing import Process

import redis as redis_lib
from loguru import logger

from app.db import session_scope
from app.models import AccountDailySummary, AccountRiskProfile
from app.crawlers.amazon_crawler.shuler.util.config import (
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB,
    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
)

ANALYSIS_INTERVAL = 6 * 3600  # 每 6 小时分析一次

# 风险维度权重
W_ERROR_RATE   = 0.30  # 错误率趋势
W_BAN_FREQ     = 0.25  # 封号频率
W_CAPTCHA_FREQ = 0.20  # 验证码频率
W_IP_DIVERSITY = 0.10  # IP 多样性（越少越高风险）
W_REGULARITY   = 0.15  # 行为规律性（标准差越小越像机器人）

# 风险等级阈值
RISK_LEVELS = [
    (25, "low"),
    (50, "medium"),
    (75, "high"),
    (100, "critical"),
]

# 配额因子映射 (risk_level → quota_factor)
QUOTA_MAP = {
    "low":      1.0,
    "medium":   0.7,
    "high":     0.45,
    "critical": 0.3,
}

# Redis key 前缀（与 ban_analyzer.py 保持一致）
ACCOUNT_QUOTA_KEY_PREFIX = "crawler:account:quota:"
ACCOUNT_QUOTA_TTL = 86400


class LongTermAnalyzer(Process):
    """
    守护进程，每 ANALYSIS_INTERVAL 秒分析一次。
    从 account_daily_summary 聚合多天数据，计算风险画像，
    写入 account_risk_profile 和 Redis 配额因子。
    """

    def __init__(self):
        super().__init__(name="LongTermAnalyzer", daemon=True)

    def run(self) -> None:
        logger.info("[LongTermAnalyzer] 启动")
        rc = self._init_redis()

        while True:
            try:
                self._analyze_all_postgres(rc)
                logger.info("[LongTermAnalyzer] 分析完成")
            except Exception:
                logger.error(f"[LongTermAnalyzer] 分析异常:\n{traceback.format_exc()}")

            time.sleep(ANALYSIS_INTERVAL)

    def _analyze_all_postgres(self, rc) -> None:
        today = datetime.utcnow().date()
        day_30_ago = today - timedelta(days=30)
        now = datetime.utcnow()
        count = 0
        with session_scope() as s:
            accounts = s.query(AccountDailySummary.username, AccountDailySummary.country).filter(
                AccountDailySummary.date >= day_30_ago
            ).distinct().all()
            if not accounts:
                logger.info("[LongTermAnalyzer] 无活跃账号数据")
                return
            for username, country in accounts:
                try:
                    profile = self._compute_risk_postgres(s, username, country or "", today)
                    if profile:
                        self._upsert_profile_postgres(s, profile, now)
                        self._update_redis_quota(rc, username, profile["risk_level"])
                        count += 1
                except Exception:
                    logger.warning(
                        f"[LongTermAnalyzer] 分析 {username} 失败: "
                        f"{traceback.format_exc()}"
                    )
        logger.info(f"[LongTermAnalyzer] 更新 {count} 条风险画像")

    @staticmethod
    def _summary_to_dict(row: AccountDailySummary) -> dict:
        return {
            "username": row.username,
            "country": row.country,
            "date": row.date,
            "total_tasks": row.total_tasks,
            "success_tasks": row.success_tasks,
            "failed_tasks": row.failed_tasks,
            "total_pages": row.total_pages,
            "total_reviews": row.total_reviews,
            "captcha_count": row.captcha_count,
            "ban_count": row.ban_count,
            "login_redirect_count": row.login_redirect_count,
            "robot_check_count": row.robot_check_count,
            "proxy_rotate_count": row.proxy_rotate_count,
            "avg_duration_seconds": row.avg_duration_seconds,
            "total_duration_seconds": row.total_duration_seconds,
            "session_count": row.session_count,
            "active_hour_distribution": row.active_hour_distribution,
            "request_interval_stddev": row.request_interval_stddev,
            "distinct_ips": row.distinct_ips,
            "distinct_asins": row.distinct_asins,
            "error_rate": row.error_rate,
        }

    def _compute_risk_postgres(self, session, username: str, country: str, today) -> dict | None:
        day_7_ago = today - timedelta(days=7)
        day_30_ago = today - timedelta(days=30)
        rows_7d = [
            self._summary_to_dict(row)
            for row in session.query(AccountDailySummary).filter(
                AccountDailySummary.username == username,
                AccountDailySummary.country == country,
                AccountDailySummary.date >= day_7_ago,
            ).order_by(AccountDailySummary.date.asc()).all()
        ]
        rows_30d = [
            self._summary_to_dict(row)
            for row in session.query(AccountDailySummary).filter(
                AccountDailySummary.username == username,
                AccountDailySummary.country == country,
                AccountDailySummary.date >= day_30_ago,
            ).order_by(AccountDailySummary.date.asc()).all()
        ]
        return self._compute_risk_from_rows(username, country, today, rows_7d, rows_30d)

    def _compute_risk_from_rows(self, username: str, country: str, today, rows_7d: list, rows_30d: list) -> dict | None:
        if not rows_7d:
            return None

        error_score = self._score_error_rate(rows_7d, rows_30d)
        ban_score = self._score_ban_freq(rows_7d)
        captcha_score = self._score_captcha_freq(rows_7d)
        ip_score = self._score_ip_diversity(rows_7d)
        regularity_score = self._score_regularity(rows_7d)

        risk_score = (
            error_score * W_ERROR_RATE
            + ban_score * W_BAN_FREQ
            + captcha_score * W_CAPTCHA_FREQ
            + ip_score * W_IP_DIVERSITY
            + regularity_score * W_REGULARITY
        )
        risk_score = max(0.0, min(100.0, risk_score))
        risk_level = "critical"
        for threshold, level in RISK_LEVELS:
            if risk_score <= threshold:
                risk_level = level
                break

        days_7 = max(1, len(rows_7d))
        return {
            "username": username,
            "country": country,
            "risk_score": round(risk_score, 2),
            "risk_level": risk_level,
            "avg_daily_error_rate_7d": round(sum(float(r.get("error_rate") or 0) for r in rows_7d) / days_7, 4),
            "avg_daily_ban_count_7d": round(sum(int(r.get("ban_count") or 0) for r in rows_7d) / days_7, 2),
            "avg_daily_captcha_count_7d": round(sum(int(r.get("captcha_count") or 0) for r in rows_7d) / days_7, 2),
            "total_days_active_30d": len(rows_30d),
            "trend_direction": self._compute_trend(rows_7d),
            "recommended_daily_budget": self._recommend_budget(risk_level),
            "recommended_page_budget": self._recommend_budget(risk_level) * 10,
            "recommended_rest_minutes": {"low": 40, "medium": 60, "high": 90, "critical": 150}.get(risk_level, 60),
        }

    @staticmethod
    def _upsert_profile_postgres(session, profile: dict, now: datetime) -> None:
        row = session.query(AccountRiskProfile).filter_by(
            username=profile["username"],
            country=profile["country"],
        ).first()
        if row is None:
            row = AccountRiskProfile(
                username=profile["username"],
                country=profile["country"],
                created_at=now,
            )
            session.add(row)
        row.risk_score = profile["risk_score"]
        row.risk_level = profile["risk_level"]
        row.avg_daily_error_rate_7d = profile["avg_daily_error_rate_7d"]
        row.avg_daily_ban_count_7d = profile["avg_daily_ban_count_7d"]
        row.avg_daily_captcha_count_7d = profile["avg_daily_captcha_count_7d"]
        row.total_days_active_30d = profile["total_days_active_30d"]
        row.trend_direction = profile["trend_direction"]
        row.recommended_daily_budget = profile["recommended_daily_budget"]
        row.recommended_page_budget = profile["recommended_page_budget"]
        row.recommended_rest_minutes = profile["recommended_rest_minutes"]
        row.last_analyzed_at = now
        row.updated_at = now

    def _analyze_all(self, cursor, conn, rc) -> None:
        """获取所有活跃账号并逐个分析"""
        today = datetime.now().date()
        day_30_ago = today - timedelta(days=30)

        # 查询近 30 天有活跃记录的账号
        cursor.execute(
            "SELECT DISTINCT username, country FROM account_daily_summary "
            "WHERE `date` >= %s",
            (day_30_ago,)
        )
        accounts = cursor.fetchall() or []
        if not accounts:
            logger.info("[LongTermAnalyzer] 无活跃账号数据")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count = 0

        for acc in accounts:
            try:
                username = acc["username"]
                country = acc["country"] or ""
                profile = self._compute_risk(cursor, username, country, today)
                if profile:
                    self._upsert_profile(cursor, conn, profile, now)
                    self._update_redis_quota(rc, username, profile["risk_level"])
                    count += 1
            except Exception:
                logger.warning(
                    f"[LongTermAnalyzer] 分析 {acc.get('username')} 失败: "
                    f"{traceback.format_exc()}"
                )

        conn.commit()
        logger.info(f"[LongTermAnalyzer] 更新 {count} 条风险画像")

    def _compute_risk(self, cursor, username: str, country: str,
                      today) -> dict | None:
        """计算单个账号的 5 维风险分"""
        day_7_ago = today - timedelta(days=7)
        day_30_ago = today - timedelta(days=30)

        # 近 7 天数据
        cursor.execute(
            "SELECT * FROM account_daily_summary "
            "WHERE username = %s AND country = %s AND `date` >= %s "
            "ORDER BY `date` ASC",
            (username, country, day_7_ago)
        )
        rows_7d = cursor.fetchall() or []

        # 近 30 天数据
        cursor.execute(
            "SELECT * FROM account_daily_summary "
            "WHERE username = %s AND country = %s AND `date` >= %s "
            "ORDER BY `date` ASC",
            (username, country, day_30_ago)
        )
        rows_30d = cursor.fetchall() or []

        if not rows_7d:
            return None

        # ── 维度 1: 错误率趋势 (0-100) ──
        error_score = self._score_error_rate(rows_7d, rows_30d)

        # ── 维度 2: 封号频率 (0-100) ──
        ban_score = self._score_ban_freq(rows_7d)

        # ── 维度 3: 验证码频率 (0-100) ──
        captcha_score = self._score_captcha_freq(rows_7d)

        # ── 维度 4: IP 多样性 (0-100, 越少 IP 越高风险) ──
        ip_score = self._score_ip_diversity(rows_7d)

        # ── 维度 5: 行为规律性 (0-100, 越规律越像机器人) ──
        regularity_score = self._score_regularity(rows_7d)

        # 加权
        risk_score = (
            error_score   * W_ERROR_RATE
            + ban_score     * W_BAN_FREQ
            + captcha_score * W_CAPTCHA_FREQ
            + ip_score      * W_IP_DIVERSITY
            + regularity_score * W_REGULARITY
        )
        risk_score = max(0.0, min(100.0, risk_score))

        # 等级
        risk_level = "critical"
        for threshold, level in RISK_LEVELS:
            if risk_score <= threshold:
                risk_level = level
                break

        # 趋势判断（近 3 天 vs 前 4 天）
        trend = self._compute_trend(rows_7d)

        # 聚合指标（7d 平均）
        days_7 = max(1, len(rows_7d))
        avg_error_7d = sum(float(r.get("error_rate") or 0) for r in rows_7d) / days_7
        avg_ban_7d = sum(int(r.get("ban_count") or 0) for r in rows_7d) / days_7
        avg_captcha_7d = sum(int(r.get("captcha_count") or 0) for r in rows_7d) / days_7

        # 建议预算
        rec_daily = self._recommend_budget(risk_level)
        rec_page  = rec_daily * 10
        rec_rest  = {"low": 40, "medium": 60, "high": 90, "critical": 150}.get(risk_level, 60)

        return {
            "username": username,
            "country": country,
            "risk_score": round(risk_score, 2),
            "risk_level": risk_level,
            "avg_daily_error_rate_7d": round(avg_error_7d, 4),
            "avg_daily_ban_count_7d": round(avg_ban_7d, 2),
            "avg_daily_captcha_count_7d": round(avg_captcha_7d, 2),
            "total_days_active_30d": len(rows_30d),
            "trend_direction": trend,
            "recommended_daily_budget": rec_daily,
            "recommended_page_budget": rec_page,
            "recommended_rest_minutes": rec_rest,
        }

    # ─── 5 维评分函数 ────────────────────────────────────────────────────────────

    @staticmethod
    def _score_error_rate(rows_7d: list, rows_30d: list) -> float:
        """
        错误率趋势评分 (0-100)。
        基于 7 天平均错误率，并考虑趋势（近 3 天 vs 前 4 天）。
        """
        if not rows_7d:
            return 0.0

        rates = [float(r.get("error_rate") or 0) for r in rows_7d]
        avg = sum(rates) / len(rates)

        # 基础分：错误率 0-50% 映射到 0-100
        base = min(100.0, avg * 200)

        # 趋势加权：近 3 天升高则 +20，近 3 天下降则 -10
        if len(rates) >= 5:
            recent = sum(rates[-3:]) / 3
            earlier = sum(rates[:-3]) / max(1, len(rates) - 3)
            if recent > earlier * 1.3:
                base = min(100.0, base + 20)
            elif recent < earlier * 0.7:
                base = max(0.0, base - 10)

        return base

    @staticmethod
    def _score_ban_freq(rows_7d: list) -> float:
        """
        封号频率评分 (0-100)。
        每天平均 0 次 = 0 分，每天 ≥3 次 = 100 分。
        """
        if not rows_7d:
            return 0.0

        total_bans = sum(int(r.get("ban_count") or 0) for r in rows_7d)
        avg_daily = total_bans / len(rows_7d)

        # 0-3 次/天 线性映射到 0-100
        return min(100.0, avg_daily / 3.0 * 100)

    @staticmethod
    def _score_captcha_freq(rows_7d: list) -> float:
        """
        验证码频率评分 (0-100)。
        每天平均 0 次 = 0 分，每天 ≥5 次 = 100 分。
        """
        if not rows_7d:
            return 0.0

        total = sum(int(r.get("captcha_count") or 0) for r in rows_7d)
        avg_daily = total / len(rows_7d)
        return min(100.0, avg_daily / 5.0 * 100)

    @staticmethod
    def _score_ip_diversity(rows_7d: list) -> float:
        """
        IP 多样性评分 (0-100, 越少 IP 越高风险)。
        7 天总不同 IP 数 ≥20 → 0 分(低风险)，≤2 → 100 分(高风险)。
        """
        if not rows_7d:
            return 50.0

        total_ips = sum(int(r.get("distinct_ips") or 0) for r in rows_7d)
        # 简化：7 天总 IP 数 2-20 线性映射 100-0
        if total_ips >= 20:
            return 0.0
        if total_ips <= 2:
            return 100.0
        return (20 - total_ips) / 18.0 * 100

    @staticmethod
    def _score_regularity(rows_7d: list) -> float:
        """
        行为规律性评分 (0-100, 越规律越像机器人)。
        使用请求间隔标准差（越小越规律 → 越高风险）和活跃时段分布集中度。
        """
        if not rows_7d:
            return 30.0

        # 请求间隔标准差：stddev 越小越可能是机器人
        stddevs = [float(r.get("request_interval_stddev") or 0) for r in rows_7d
                    if r.get("request_interval_stddev") is not None]
        if stddevs:
            avg_stddev = sum(stddevs) / len(stddevs)
            # stddev < 2s → 非常规律(高风险), > 30s → 随机(低风险)
            if avg_stddev <= 2:
                interval_score = 100.0
            elif avg_stddev >= 30:
                interval_score = 0.0
            else:
                interval_score = (30 - avg_stddev) / 28.0 * 100
        else:
            interval_score = 30.0  # 无数据取默认

        # 活跃时段集中度：只在少数小时活跃 → 更像机器人
        # 统计 7 天内有活跃记录的不同小时数
        active_hours = set()
        for r in rows_7d:
            dist = r.get("active_hour_distribution")
            if isinstance(dist, str):
                try:
                    import json
                    dist = json.loads(dist)
                except Exception:
                    dist = {}
            if isinstance(dist, dict):
                active_hours.update(dist.keys())

        unique_hours = len(active_hours)
        # 活跃 ≤3 小时 → 高风险, ≥12 小时 → 低风险
        if unique_hours <= 3:
            hour_score = 90.0
        elif unique_hours >= 12:
            hour_score = 10.0
        else:
            hour_score = 90 - (unique_hours - 3) / 9.0 * 80

        return interval_score * 0.6 + hour_score * 0.4

    # ─── 辅助函数 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_trend(rows_7d: list) -> str:
        """判断近 7 天的风险趋势方向"""
        if len(rows_7d) < 4:
            return "stable"

        recent_rates = [float(r.get("error_rate") or 0) for r in rows_7d[-3:]]
        earlier_rates = [float(r.get("error_rate") or 0) for r in rows_7d[:-3]]

        recent_avg = sum(recent_rates) / len(recent_rates)
        earlier_avg = sum(earlier_rates) / max(1, len(earlier_rates))

        if earlier_avg == 0:
            return "stable" if recent_avg == 0 else "worsening"
        ratio = recent_avg / earlier_avg
        if ratio > 1.3:
            return "worsening"
        elif ratio < 0.7:
            return "improving"
        return "stable"

    @staticmethod
    def _recommend_budget(risk_level: str) -> int:
        """根据风险等级推荐每日任务预算"""
        return {
            "low":      60,
            "medium":   40,
            "high":     20,
            "critical": 10,
        }.get(risk_level, 40)

    def _upsert_profile(self, cursor, conn, profile: dict, now: str) -> None:
        """Upsert 到 account_risk_profile"""
        sql = """
            INSERT INTO account_risk_profile
            (username, country, risk_score, risk_level,
             avg_daily_error_rate_7d, avg_daily_ban_count_7d, avg_daily_captcha_count_7d,
             total_days_active_30d, trend_direction,
             recommended_daily_budget, recommended_page_budget, recommended_rest_minutes,
             last_analyzed_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                risk_score=VALUES(risk_score),
                risk_level=VALUES(risk_level),
                avg_daily_error_rate_7d=VALUES(avg_daily_error_rate_7d),
                avg_daily_ban_count_7d=VALUES(avg_daily_ban_count_7d),
                avg_daily_captcha_count_7d=VALUES(avg_daily_captcha_count_7d),
                total_days_active_30d=VALUES(total_days_active_30d),
                trend_direction=VALUES(trend_direction),
                recommended_daily_budget=VALUES(recommended_daily_budget),
                recommended_page_budget=VALUES(recommended_page_budget),
                recommended_rest_minutes=VALUES(recommended_rest_minutes),
                last_analyzed_at=VALUES(last_analyzed_at),
                updated_at=VALUES(updated_at)
        """
        cursor.execute(sql, (
            profile["username"], profile["country"],
            profile["risk_score"], profile["risk_level"],
            profile["avg_daily_error_rate_7d"],
            profile["avg_daily_ban_count_7d"],
            profile["avg_daily_captcha_count_7d"],
            profile["total_days_active_30d"],
            profile["trend_direction"],
            profile["recommended_daily_budget"],
            profile["recommended_page_budget"],
            profile["recommended_rest_minutes"],
            now, now, now,
        ))

    @staticmethod
    def _update_redis_quota(rc, username: str, risk_level: str) -> None:
        """将风险等级对应的配额因子写入 Redis，与 AccountScheduler._get_quota_factor() 对接"""
        try:
            factor = QUOTA_MAP.get(risk_level, 1.0)
            key = ACCOUNT_QUOTA_KEY_PREFIX + username
            # 只降不升：如果当前配额因子更低（来自 ban_analyzer 短期处罚），不覆盖
            cur = rc.get(key)
            if cur is not None:
                cur_val = float(cur)
                if cur_val < factor:
                    return  # 短期处罚更严格，不覆盖
            rc.set(key, str(round(factor, 3)), ex=ACCOUNT_QUOTA_TTL)
        except Exception:
            pass

    @staticmethod
    def _init_mysql():
        import mysql.connector
        try:
            return mysql.connector.connect(
                host=MYSQL_HOST, port=MYSQL_PORT,
                user=MYSQL_USER, password=MYSQL_PASSWORD,
                database=MYSQL_DB, autocommit=False,
            )
        except Exception as exc:
            logger.warning(f"[LongTermAnalyzer] MySQL 连接不可用，跳过长期分析: {exc}")
            return None

    @staticmethod
    def _init_redis():
        return redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT,
            username=REDIS_USERNAME, password=REDIS_PASSWORD,
            db=REDIS_DB, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
