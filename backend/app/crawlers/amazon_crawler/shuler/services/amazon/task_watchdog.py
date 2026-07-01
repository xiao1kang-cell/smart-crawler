import argparse
import os
import time
import traceback
from datetime import datetime

import requests
from loguru import logger

from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message


class ApiWatchdog:
    def __init__(
        self,
        base_url: str,
        interval_seconds: int = 15,
        timeout_seconds: int = 5,
        fail_threshold: int = 3,
        alert_cooldown_seconds: int = 300,
    ):
        self.base_url = base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self.fail_threshold = fail_threshold
        self.alert_cooldown_seconds = alert_cooldown_seconds

        self._fail_count = 0
        self._last_alarm_ts = 0.0

    def _should_alarm(self) -> bool:
        now = time.time()
        if now - self._last_alarm_ts < self.alert_cooldown_seconds:
            return False
        self._last_alarm_ts = now
        return True

    def _send_alarm(self, msg: str):
        if not self._should_alarm():
            return
        try:
            send_custom_robot_group_message(
                f"[独立监控告警] {msg}",
                at_mobiles=["17398238551"],
            )
        except Exception:
            logger.error(f"告警发送失败: {traceback.format_exc()}")

    def _check_heartbeat(self) -> bool:
        url = f"{self.base_url}/heartbeat"
        resp = requests.get(url, timeout=self.timeout_seconds)
        if resp.status_code != 200:
            raise Exception(f"heartbeat status={resp.status_code}, body={resp.text[:200]}")
        data = resp.json()
        return bool(data.get("alive"))

    def _check_health(self) -> dict:
        url = f"{self.base_url}/health"
        resp = requests.get(url, timeout=self.timeout_seconds)
        if resp.status_code != 200:
            raise Exception(f"health status={resp.status_code}, body={resp.text[:200]}")
        return resp.json()

    def run_forever(self):
        logger.info(
            f"watchdog started: base_url={self.base_url}, interval={self.interval_seconds}s, threshold={self.fail_threshold}"
        )
        while True:
            try:
                alive = self._check_heartbeat()
                if not alive:
                    raise Exception("heartbeat alive=False")

                health_data = self._check_health()
                status = health_data.get("status")
                if status != "ok":
                    self._fail_count += 1
                    logger.warning(f"health degraded: {health_data}")
                    if self._fail_count >= self.fail_threshold:
                        self._send_alarm(f"health degraded after {self._fail_count} checks: {health_data}")
                else:
                    if self._fail_count > 0:
                        logger.info("service recovered")
                    self._fail_count = 0

                logger.info(
                    f"monitor pass at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, fail_count={self._fail_count}"
                )
            except Exception as exc:
                self._fail_count += 1
                logger.error(f"monitor failed: {exc}")
                if self._fail_count >= self.fail_threshold:
                    self._send_alarm(
                        f"API heartbeat/health failed {self._fail_count} times, base_url={self.base_url}, error={exc}"
                    )

            time.sleep(self.interval_seconds)


def parse_args():
    parser = argparse.ArgumentParser(description="Independent watchdog for task_api service")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="task_api base url")
    parser.add_argument("--interval", type=int, default=15, help="check interval seconds")
    parser.add_argument("--timeout", type=int, default=5, help="request timeout seconds")
    parser.add_argument("--fail-threshold", type=int, default=3, help="consecutive failures before alarm")
    parser.add_argument("--alert-cooldown", type=int, default=300, help="alarm cooldown seconds")
    return parser.parse_args()


def setup_logger():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logger.add(
        os.path.join(log_dir, "task_watchdog_{time}.log"),
        rotation="1 day",
        retention="14 days",
        encoding="utf-8",
        level="INFO",
    )


if __name__ == "__main__":
    setup_logger()
    args = parse_args()
    watchdog = ApiWatchdog(
        base_url=args.base_url,
        interval_seconds=args.interval,
        timeout_seconds=args.timeout,
        fail_threshold=args.fail_threshold,
        alert_cooldown_seconds=args.alert_cooldown,
    )
    watchdog.run_forever()
