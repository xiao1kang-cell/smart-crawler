"""
Amazon评论爬虫配置文件
基于CSDN博客: https://blog.csdn.net/mueam_ym/article/details/150956640
"""
import os

# 站点映射配置
SITE_MAPPING = {
    "US": "www.amazon.com",      # 🇺🇸 美国站
    "UK": "www.amazon.co.uk",    # 🇬🇧 英国站
    "DE": "www.amazon.de",       # 🇩🇪 德国站
    "JP": "www.amazon.co.jp",    # 🇯🇵 日本站
    "CA": "www.amazon.ca",       # 🇨🇦 加拿大站
    "FR": "www.amazon.fr",       # 🇫🇷 法国站
    "IT": "www.amazon.it",       # 🇮🇹 意大利站
    "ES": "www.amazon.es",       # 🇪🇸 西班牙站
    "AU": "www.amazon.com.au",   # 🇦🇺 澳大利亚站
    "IN": "www.amazon.in",       # 🇮🇳 印度站
    "BR": "www.amazon.com.br",   # 🇧🇷 巴西站
    "MX": "www.amazon.com.mx",  # 🇲🇽 墨西哥站
    "SG": "www.amazon.sg",      # 🇸🇬 新加坡站
    "SE": "www.amazon.se",      # 🇸🇪 瑞典站
    "AE": "www.amazon.ae",      # 🇦🇪 阿联酋站
    "NL": "www.amazon.nl",      # 🇳🇱 荷兰站
    "PL": "www.amazon.pl",      # 🇵🇱 波兰站
    "BE": "www.amazon.com.be",   # 🇧🇪 比利时站
    "SA": "www.amazon.sa",  # 🇸🇦 沙特
    "TR": "www.amazon.com.tr",  # 🇹🇷 土耳其
}
COUNTRY_MAPPING = {
    "US": ["united states", "usa", "america", "den vereinigten staaten", "stati uniti", "estados unidos", "états-unis", "الولايات المتحدة"],
    "UK": [
        "united kingdom", "britain", "great britain", "england",
        "großbritannien", "grossbritannien", "vereinigtes königreich",
        "vereinigtes konigreich", "vereinigten königreich",
        "regno unito", "reino unido", "royaume-uni", "irland", "ireland"
    ],
    "DE": ["germany", "deutschland", "germania", "alemania", "allemagne", "alemanha"],
    "JP": ["japan", "nippon", "giappone", "japón", "japon", "japão"],
    "CA": ["canada", "kanada", "canadá", "كندا"],
    "FR": ["france", "french", "frankreich", "francia", "frança"],
    "IT": ["italy", "italien", "italia", "italie", "itália"],
    "ES": ["spain", "españa", "spanien", "spagna", "espagne", "espanha"],
    "AU": ["australia", "australien", "australie", "austrália"],
    "IN": ["india", "indien", "inde", "índia"],
    "BR": ["brazil", "brasilien", "brasile", "brésil", "brasil"],
    "MX": ["mexico", "mexiko", "messico", "mexique", "México", "méxico"],
    "SG": ["singapore", "singapur", "singapour", "singapura"],
    "SE": ["sweden", "schweden", "svezia", "suecia", "suède", "suécia"],
    "AE": ["united arab emirates", "vereinigten arabischen emiraten", "emiratos árabes", "emirati arabi", "émirats arabes", "emirados árabes unidos", "الإمارات العربية المتحدة", "الإمارات"],
    "NL": ["netherlands", "niederlanden", "holland", "paesi bassi", "países bajos", "pays-bas", "países baixos"],
    "PL": ["poland", "polen", "polonia", "pologne", "polônia"],
    "BE": ["belgium", "belgien", "belgio", "bélgica", "belgique"],
    "SA": ["saudi arabia", "saudi-arabien", "arabia saudita", "arabie saoudite", "المملكة العربية السعودية", "arábia saudita"],
    "EG": ["egypt", "ägypten", "egitto", "egipto", "égypte", "egito"],
    "ZA": ["south africa", "südafrika", "sudafrica", "afrique du sud", "sudáfrica", "africa do sul"],
    "TR": ["turkey", "türkei", "turchia", "turquía", "turquie", "türkiye", "türkiye'de"],
    }
PROXY_MAPPING = {
    "US": "us",      # 🇺🇸 美国站
    "UK": "gb",    # 🇬🇧 英国站
    "DE": "de",       # 🇩🇪 德国站
    "JP": "jp",    # 🇯🇵 日本站
    "CA": "ca",       # 🇨🇦 加拿大站
    "FR": "fr",       # 🇫🇷 法国站
    "IT": "it",       # 🇮🇹 意大利站
    "ES": "es",       # 🇪🇸 西班牙站
    "AU": "au",   # 🇦🇺 澳大利亚站
    "IN": "in",       # 🇮🇳 印度站
    "BR": "br",   # 🇧🇷 巴西站
    "MX": "mx" ,   # 🇲🇽 墨西哥站
    "AE": "ae",    # 🇦🇪 阿联酋站
    'SA': "sa" #沙特
}
# 爬虫配置
CRAWLER_CONFIG = {
    "max_pages": 10,           # 📄 每个ASIN最大页数
    "page_delay": (2, 4),     # ⏱️ 页面间隔(秒)
    "asin_delay": (5, 10),    # 🔄 ASIN间隔(秒)
    "request_timeout": 25,    # ⏰ 请求超时
    "max_retries": 3,         # 🔁 最大重试次数
    "concurrent_requests": 5  # 🚀 并发请求数
}


# === 任务/账号策略配置 ===
MAX_PROCESSES=2          # 最大并发进程数（动态调度模式下由 DynamicWorkerManager 覆盖）
MAX_PER_HOUR=40         # 账号每小时最大使用次数
MAX_PER_MINUTE=1        # 账号每分钟最大使用次数
MAX_FAIL=2              # 账号连续失败多少次进入冷却（降低至 2，Amazon 敏感度高）
COOLDOWN_SECONDS=60*30   # 账号冷却时间（秒）（提升至 1 小时，避免频繁触发检测）
TASK_BATCH_SIZE=1      # 每次从MySQL拉取的任务批次
RETRY_TIMES=6           # 单个ASIN任务失败重试次数
TASK_TIMEOUT_MINUTES = TASK_BATCH_SIZE*4   # 任务超时时间（分钟）；单任务最多含 10 页×多星级，留足余量
ACCOUNT_USED_MINUTES = TASK_TIMEOUT_MINUTES*10   # 账号使用超时时间（分钟）；与 TASK_TIMEOUT_MINUTES 对齐，防止虚假释放
REFRESH_TIME = 24*15       #COOKIES刷新时间（小时）
SINGLE_TASK_NEED_CRAWLER_DELAY_MINUTES = 10  # single 模式仅拉取 need_crawler_time 超过该分钟数的任务
SINGLE_TASK_HARD_TIMEOUT_SECONDS = int(os.getenv("SINGLE_TASK_HARD_TIMEOUT_SECONDS", "1200"))

# === 压力测试参数 ===
STRESS_TEST_LABEL = 'stress_test'           # 测试账号在 crawler_accounts.label 中的标记
STRESS_TEST_DAILY_PAGE_TARGET = 200         # ← 每次迭代只改这一个值（每账号每日目标页面数）
STRESS_TEST_DAILY_TASK_MAX = 100            # 每账号每日最多任务数上限（兜底保护）
STRESS_TEST_REST_MIN_SECONDS = 30           # 测试账号会话间最少休息（秒）
STRESS_TEST_REST_MAX_SECONDS = 120          # 测试账号会话间最多休息（秒）
# 压测档位表：每个元素 (每日任务上限, 在该档位停留天数)
# 停留天数=0 表示一直停在该档位（最终兜底）
STRESS_TEST_SCHEDULE = [
    (50,  1),   # 第1天
    (60,  1),   # 第2~3天
    (70,  1),   # 第4~7天
    (80,  4),   # 第8~12天
    (90,  6),   # 第13~18天
    (100, 0),   # 第19天起封顶
]

# === 风控优化参数 ===
RETRY_BACKOFF_BASE = 2.0      # 指数退避基数（秒），第 n 次重试等待 base^n 秒
RETRY_BACKOFF_MAX = 60.0      # 重试等待上限（秒）
STAR_PASS_DELAY_MIN = 8      # 不同星级筛选间的最小延迟（秒）
STAR_PASS_DELAY_MAX = 15      # 不同星级筛选间的最大延迟（秒）

# === 动态 worker 调度参数 ===
WORKER_SCALE_INTERVAL = 60    # 每隔多少秒重新评估 worker 数量（秒）
WORKER_MIN_PER_COUNTRY = 1    # 每个国家最少保留的 worker 数
WORKER_MAX_PER_COUNTRY = 20   # 每个国家最多允许的 worker 数
WORKER_TASKS_PER_WORKER = 50  # 每个 worker 对应多少待处理任务（用于动态计算所需 worker 数）
WORKER_OFF_HOUR_SCALE = 0.3   # 非活跃时段 worker 缩减比例（保留 30%）

# === InfluxDB 可观测性配置 ===
# 在 .env 文件中配置以下变量（不配置则降级为 ConsoleSink，不影响运行）：
#   INFLUXDB_URL=http://localhost:8086
#   INFLUXDB_TOKEN=your-token
#   INFLUXDB_ORG=crawler
#   INFLUXDB_BUCKET=crawler
# Docker 快速部署：
#   docker run -d -p 8086:8086 --name influxdb influxdb:2.7
#   docker run -d -p 3000:3000 --name grafana grafana/grafana
import os as _os
INFLUXDB_URL    = _os.getenv("INFLUXDB_URL", "")
INFLUXDB_TOKEN  = _os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_ORG    = _os.getenv("INFLUXDB_ORG", "crawler")
INFLUXDB_BUCKET = _os.getenv("INFLUXDB_BUCKET", "crawler")
