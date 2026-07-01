import copy
import gc
import json
import math
import os
import random
import re
import time
import httpx
import traceback
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_base import AmazonBase, CookieRefreshExhaustedException, \
    AccountSwitchRequiredException, NetworkException, BIT_API_BASE
# from amazon_config import *
from multiprocessing import Process
from retrying import retry

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import *
from app.crawlers.amazon_crawler.shuler.services.amazon.review_parser_utils import (
    add_current_format_param,
    add_recent_sort_param,
    alert_review_parse_error,
    parse_review_block_html,
    parse_review_date,
    should_use_current_format_filter,
    should_use_recent_sort,
)
# from app.crawlers.amazon_crawler.shuler.util.account_manage import AccountManager
from app.crawlers.amazon_crawler.shuler.util.account_scheduler import HumanLikeAccountManager as AccountManager

from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from app.crawlers.amazon_crawler.shuler.util.event_logger import push_event, EventType
from app.crawlers.amazon_crawler.shuler.util.ban_analyzer import (
    increment_account_error, reset_account_error,
    BanReason,
    increment_network_error, get_network_error_count,
    should_alert_network_error, mark_network_alert_sent,
    NETWORK_ERR_MULTI_THRESHOLD, NETWORK_ERR_ALERT_THRESHOLD,
)
from app.crawlers.amazon_crawler.shuler.util.influxdb_sink import get_reporter

try:
    from app.crawlers.amazon_crawler.shuler.util.mongo_ import MongoAccountDB
except ImportError:
    MongoAccountDB = None
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message

httpx._config.DEFAULT_CIPHERS = 'TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA'


class Reviews(AmazonBase):

    def __init__(self, account_info, task=None):
        AmazonBase.__init__(self, account_info, task)
        self.reviews_csrf_token = ''
        self._log = logger   # 默认裸 logger；get_reviews_main() 里会替换为 bind 版本
        self._reset_review_integrity_stats()

    def _reset_review_integrity_stats(self):
        self._review_page_keys = set()
        self._review_raw_slots_count = 0
        self._review_duplicate_slots_count = 0
        self._review_invalid_slots_count = 0
        self._review_expected_pages_seen = 0
        self._review_expected_pages_total = 0
        self._review_page_completion_ok = True

    def _record_review_page_stats(self, resp, raw_count, parsed_count, duplicate_count, invalid_count):
        if raw_count <= 0:
            return
        page_key = "|".join(map(str, [
            resp.get("filter_star", ""),
            resp.get("page", ""),
            resp.get("url", ""),
        ]))
        if page_key in self._review_page_keys:
            return
        self._review_page_keys.add(page_key)
        self._review_raw_slots_count += int(raw_count or 0)
        self._review_duplicate_slots_count += int(duplicate_count or 0)
        self._review_invalid_slots_count += int(invalid_count or 0)
        self._log.info(
            f"[完整性] page={resp.get('page', '-')} star={resp.get('filter_star', '')} "
            f"blocks={raw_count}, parsed={parsed_count}, duplicate={duplicate_count}, invalid={invalid_count}"
        )

    def _record_review_page_completion(self, pages_seen, expected_pages):
        expected_pages = max(int(expected_pages or 0), 0)
        pages_seen = max(int(pages_seen or 0), 0)
        if expected_pages <= 0:
            return
        self._review_expected_pages_total += expected_pages
        self._review_expected_pages_seen += min(pages_seen, expected_pages)
        if pages_seen < expected_pages:
            self._review_page_completion_ok = False


    # def save_reviews_append(self, reviews, base_filename: str = "amazon_reviews_append"):
    #     """
    #     追加保存单个ASIN的评论数据
    #     :param reviews: 单个ASIN的评论列表
    #     :param base_filename: 基础文件名
    #     """
    #     if not reviews:
    #         print("⚠️  无评论数据可追加保存")
    #         return
    #
    #     # 统一字段（避免字段缺失）
    #     all_fields = {'ASIN', '评论人', '标题', '内容', '地区', '日期', '产品属性', '购买类型', '评分'}
    #
    #     # 1. JSON追加（先读再写）
    #     json_filename = f"{base_filename}.json"
    #     try:
    #         existing_data = []
    #         # 如果文件已存在，读取原有数据
    #         if os.path.exists(json_filename) and os.path.getsize(json_filename) > 0:
    #             with open(json_filename, 'r', encoding='utf-8') as f:
    #                 existing_data = json.load(f)
    #         # 合并新数据
    #         existing_data.extend(reviews)
    #         # 重新写入
    #         with open(json_filename, 'w', encoding='utf-8') as f:
    #             json.dump(existing_data, f, ensure_ascii=False, indent=2)
    #         print(f"✅ JSON追加完成：{json_filename}（新增 {len(reviews)} 条）")
    #     except Exception as e:
    #         print(f"❌ JSON追加失败：{str(e)}")
    #
    #     # # 2. CSV追加（直接追加行，首次写表头）
    #     # csv_filename = f"{base_filename}.csv"
    #     # try:
    #     #     file_exists = os.path.exists(csv_filename) and os.path.getsize(csv_filename) > 0
    #     #     with open(csv_filename, 'a', encoding='utf-8-sig', newline='') as f:
    #     #         writer = csv.DictWriter(f, fieldnames=sorted(all_fields))
    #     #         # 首次运行写入表头
    #     #         if not file_exists:
    #     #             writer.writeheader()
    #     #         # 追加新行
    #     #         for review in reviews:
    #     #             row = {field: review.get(field, '') for field in all_fields}
    #     #             writer.writerow(row)
    #     #     print(f"✅ CSV追加完成：{csv_filename}（新增 {len(reviews)} 条）")
    #     except Exception as e:
    #         print(f"❌ CSV追加失败：{str(e)}")

    def login_amazon(self):
        # 用注入cookies的方式试试
        country = ((self.task or {}).get("country") or getattr(self.account_info, "country", "") or "US").upper()
        site = SITE_MAPPING.get(country, SITE_MAPPING["US"])
        cookie_domain = f".{site.replace('www.', '')}"
        self.page.get(f"https://{site}")
        cookies = getattr(self.account_info, "cookies", {}) or {}
        cookie_items = cookies.items() if isinstance(cookies, dict) else cookies
        for name, value in cookie_items:
            self.chrome.driver.add_cookie({'name': name, 'value': value, 'domain': cookie_domain})
        # 刷新页面以应用 Cookies
        self.page.refresh()
        time.sleep(2)
        if 'sign' not in self.page.ele('#nav-link-accountList-nav-line-1').text:
            print('登录成功')
            return True

    def verify_asin_is_error(self, task):
        if self.page.ele('@src=https://images-na.ssl-images-amazon.com/images/G/01/error/en_US/title._TTD_.png',
                         timeout=1):
            print(f'页面错误，初步查看站点是否正常:{task}')
            self.page.get(f'https://{SITE_MAPPING[task["country"]]}/dp/{task["asin"]}')
            time.sleep(1)
            if self.page.ele('@src=https://images-na.ssl-images-amazon.com/images/G/01/error/en_US/title._TTD_.png',
                             timeout=1):
                print(f'页面错误，查看站点是否正常:{task}')
                return False, []
            else:
                more_review = self.page.ele('@data-hook=see-all-reviews-link-foot', timeout=2)
                if more_review:
                    more_review.click()
                    time.sleep(1)
                    return True, []
                else:
                    print('评论只有一页')
                    one_pages = self.parse_reviews(task["asin"], page=1)

                    return False, one_pages

        else:
            return True, []


    def get_star_filter_value(self, star):
        """将星级数字转换为亚马逊 API 参数值"""
        star_mapping = {
            5: 'five_star',
            4: 'four_star',
            3: 'three_star',
            2: 'two_star',
            1: 'one_star'
        }
        return star_mapping.get(star, '')

    @retry(stop_max_attempt_number=3, wait_random_min=1000, wait_random_max=2000, )
    def get_more_reviews(self, task, page, seen_review_ids, all_datas, is_exists_type, filter_star='', next_page_token=None):
        try:
            asin = task["asin"] if 'new_asin' not in task.keys() else task["new_asin"]
            is_exists_type = bool(is_exists_type or should_use_current_format_filter(task))

            # 页内翻页保持稳定；BanAnalyzer 降速只放大任务间隔，避免 50 页任务被拖到超时。
            _base_sleep = random.uniform(5, 8)
            time.sleep(_base_sleep)
            #recent 最近时间排序 '' 系统默认排序
            sortBy = 'recent' if should_use_recent_sort(task, getattr(self, "date_cutoff", None)) else ''

            # 构造分页参数（亚马逊AJAX翻页的标准参数）
            ajax_headers = {
                "accept": "text/html,*/*",
                "accept-language": "zh-CN,zh;q=0.9",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                "device-memory": "8",
                "downlink": "1.45",
                "dpr": "2",
                "ect": "3g",
                "origin": f"https://{SITE_MAPPING[task['country'].upper()]}",
                "priority": "u=1, i",
                "rtt": "300",
                "sec-ch-device-memory": "8",
                "sec-ch-dpr": "2",
                "sec-ch-ua": "\"Not(A:Brand\";v=\"8\", \"Chromium\";v=\"144\", \"Google Chrome\";v=\"144\"",
                "sec-ch-ua-full-version-list": "\"Not(A:Brand\";v=\"8.0.0.0\", \"Chromium\";v=\"144.0.7559.110\", \"Google Chrome\";v=\"144.0.7559.110\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-viewport-width": "1399",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "User-Agent": '',
                "viewport-width": "1399",
                "x-requested-with": "XMLHttpRequest"
            }
            ajax_headers.update(self.generate_sec_ch_headers())
            ajax_headers['anti-csrftoken-a2z'] = self.reviews_csrf_token

            # 判断使用新方式（nextPageToken）还是旧方式（pageNumber）
            if next_page_token and page > 1:
                # 新分页方式：使用 nextPageToken
                data = {
                    "sortBy": sortBy,
                    "reviewerType": "",
                    "formatType": "current_format" if is_exists_type else "",
                    "mediaType": "",
                    "filterByStar": filter_star or "all_stars",
                    "filterByAge": "",
                    "pageNumber": '1',
                    "filterByLanguage": "",
                    "filterByKeyword": "",
                    "nextPageToken": '',
                    "shouldAppend": "true",
                    "deviceType": "desktop",
                    "canShowIntHeader": "true",
                    "reviewsShown": '' ,
                    "reftag": "cm_cr_getr_d_paging_btm",
                    "pageSize": "10",
                    "asin": asin,
                    "scope": "reviewsAjax3"
                }
                #{'shouldAppend': 'true', 'deviceType': 'desktop', 'canShowIntHeader': 'false', 'nextPageToken': 'MjAyNi0wMy0yNVQwMzo0Nzo1My40MjA2NTM4NDBaADEw', 'pageNumber': '2'}
                data.update(next_page_token)
                # if data['pageNumber'] != str(page):
                #     send_custom_robot_group_message(f'page不一致 ：{page}-{next_page_token}-{self.task}')
                #     data['pageNumber'] = str(page)

                # url = "https://www.amazon.com/portal/customer-reviews/ajax/reviews/get/ref=cm_cr_getr_d_paging_btm"
                #referer =https://www.amazon.com/product-reviews/B0CTCPLJQP/ref=cm_cr_arp_d_paging_btm?ie=UTF8&pageNumber=2&nextPageToken=MjAyNi0wMy0yNlQwNzowNjo0MS41NzAwMDgwMTNaADEw
                ajax_base_url = f"https://{SITE_MAPPING[task['country'].upper()]}/portal/customer-reviews/ajax/reviews/get/ref=cm_cr_getr_d_paging_btm"
                prev_token_obj = self.page_token if isinstance(getattr(self, 'page_token', None), dict) else None
                prev_page =  prev_token_obj.get('pageNumber') if isinstance(prev_token_obj, dict) else str(max(1, page - 1))
                prev_token = prev_token_obj.get('nextPageToken') if isinstance(prev_token_obj, dict) else None
                referer_url = f"https://{SITE_MAPPING[task['country'].upper()]}/product-reviews/{asin}/ref=cm_cr_arp_d_paging_btm?ie=UTF8&pageNumber={prev_page}"
                if prev_token:
                    referer_url = f"{referer_url}&nextPageToken={prev_token}"
                referer_url = add_current_format_param(referer_url, is_exists_type)
                ajax_headers['referer'] = referer_url
                # print(referer_url)
            elif page == 1:
                data = {
                    "sortBy": sortBy,
                    "reviewerType": "",
                    "formatType": "current_format" if is_exists_type else "",
                    "mediaType": "",
                    "filterByStar": filter_star,
                    "filterByAge": "",
                    "pageNumber": str(page),
                    "filterByLanguage": "",
                    "filterByKeyword": "",
                    "shouldAppend": "undefined",
                    "deviceType": "desktop",
                    "canShowIntHeader": "undefined",
                    "reftag": "cm_cr_arp_d_viewopt_srt",
                    "pageSize": "10",
                    "asin": asin,
                    "scope": "reviewsAjax0"
                }
                ajax_base_url = f"https://{SITE_MAPPING[task['country'].upper()]}/portal/customer-reviews/ajax/reviews/get/"
                ajax_headers['referer'] = self.first_page_url
            else:
                # 旧分页方式
                data = {
                    "sortBy": sortBy,
                    "reviewerType": "all_reviews",
                    "formatType": "" if not is_exists_type else 'current_format',
                    "mediaType": "",
                    "filterByStar": filter_star,
                    "filterByAge": "",
                    "pageNumber": str(page),
                    "filterByLanguage": "",
                    "filterByKeyword": "",
                    "shouldAppend": "undefined",
                    "deviceType": "desktop",
                    "canShowIntHeader": "undefined",
                    "reftag": f"cm_cr_getr_d_paging_btm_next_{page}",
                    "pageSize": "10",
                    "asin": asin,
                    "scope": "reviewsAjax0"
                }
                ajax_base_url = f"https://{SITE_MAPPING[task['country'].upper()]}/portal/customer-reviews/ajax/reviews/get/"
                referer_url = f"https://{SITE_MAPPING[task['country'].upper()]}/product-reviews/{asin}/ref=cm_cr_dp_d_show_all_btm?ie=UTF8&reviewerType=all_reviews&pageNumber={page - 1}"
                ajax_headers['referer'] = add_current_format_param(referer_url, is_exists_type)

            # 发送POST请求
            ajax_response = self.request('post',
                                         url=ajax_base_url,
                                         headers=ajax_headers,
                                         proxies=self.proxies,
                                         data=data,
                                         timeout=25,
                                         verify=False,
                                         )
            if 'Click the button below to continue shopping' in ajax_response.text:
                print('tsl反爬')
                time.sleep(5)
                raise Exception('tsl反爬')
            if ajax_response.status_code == 200:
                # 亚马逊AJAX响应是HTML片段，直接解析
                if page == 1:  # 从变体评论第一页出获取评论总数，算出pages
                    data_, next_token = self.parse_reviews_ajax(
                        {"resp": ajax_response.text, 'url': ajax_base_url, 'page': page, 'filter_star': filter_star},
                        task,
                        seen_review_ids,
                    )
                else:
                    data_, next_token = self.parse_reviews_ajax(
                        {"resp": ajax_response.text, 'url': ajax_base_url, 'page': page, 'filter_star': filter_star},
                        task,
                        seen_review_ids,
                    )
                all_datas.extend(data_)
                self._log.info(
                    f"[翻页] page={page} star={filter_star} parsed={len(data_)} "
                    f"bytes={len(ajax_response.text)} token_in={bool(next_page_token)} token_out={bool(next_token)}"
                )

                # 发射 PAGE_FETCHED 事件
                try:
                    _redis = self._get_redis()
                    try:
                        _real_ip = self._resolve_proxy_ip()
                    except Exception:
                        _real_ip = ''
                    push_event(_redis, EventType.PAGE_FETCHED,
                               username=getattr(self.account_info, 'username', ''),
                               asin=task.get('asin', ''), country=task.get('country', ''),
                               page=page, http_status=ajax_response.status_code,
                               worker_id=str(self.worker_id or ''),
                               proxy=_real_ip)
                except Exception:
                    pass

                # 如果解析返回了 token，说明有新分页方式
                if next_token:
                    return next_token
            else:
                # 发射 PAGE_FAILED 事件
                try:
                    _redis = self._get_redis()
                    push_event(_redis, EventType.PAGE_FAILED,
                               username=getattr(self.account_info, 'username', ''),
                               asin=task.get('asin', ''), country=task.get('country', ''),
                               page=page, http_status=ajax_response.status_code,
                               worker_id=str(self.worker_id or ''),
                               error_msg=f"HTTP {ajax_response.status_code}")
                except Exception:
                    pass
                print(f"❌ ASIN [{task['asin']}] 第{page}页请求失败，状态码：{ajax_response.status_code}")
                raise

            # 反爬延迟
        except:
            logger.error(f"❌ ASIN [{task['asin']}] 第{page}页爬取异常：{traceback.format_exc()}")
            time.sleep(4)
            raise

    def _extract_next_page_token(self, soup):
        """
        从亚马逊AJAX响应中提取 nextPageToken
        新分页方式：找到 show-more-button 元素，解析 data-reviews-state-param 属性的 JSON 数据
        """
        try:
            # 检查是否存在 show-more-button（新分页方式的标志）
            # 找到 show-more-button 元素
            show_more_button = soup.find(attrs={'data-hook': 'show-more-button'})
            if not show_more_button:
                return None

            # 获取 data-reviews-state-param 属性值
            state_param = show_more_button.get('data-reviews-state-param')
            if not state_param:
                logger.warning(f"【分页检测】找到 show-more-button 但无 data-reviews-state-param 属性 ")
                return None

            # 属性值是 HTML 转义的 JSON，先替换 &quot; 为 "
            json_str = state_param.replace('&quot;', '"')

            # 解析 JSON 提取 nextPageToken
            state_data = json.loads(json_str)
            next_token = state_data.get('nextPageToken')

            if not next_token:
                # 找到 button 但无 token，打印上下文
                logger.warning(f"【分页检测】找到 show-more-button 但 JSON 中无 nextPageToken，data-reviews-state-param={state_param}")
                return None

            return state_data

        except json.JSONDecodeError as e:
            logger.error(f"【分页检测】解析 data-reviews-state-param JSON 失败: {e}")
            return None
        except Exception as e:
            logger.error(f"【分页检测】提取 nextPageToken 异常: {e}")
            return None

    def parse_reviews_date(self, review_data, block):
        parse_review_date(review_data, block, self.task.get("country", ""), logger)

    def get_site(self,country_code):
        # 模糊匹配：检查输入是否包含任一关键词
        for code, keywords in COUNTRY_MAPPING.items():
            for keyword in keywords:
                if keyword.lower() in country_code.lower():
                    return code
        # 无独立站点的国家（如 Polen/Belgien/Österreich），回退到任务本身的站点
        fallback = self.task.get('country', '').upper()
        logger.warning(f"未匹配站点，country_code={country_code}，回退到任务站点={fallback}")
        return fallback

    def parse_reviews_ajax(self, resp, task, seen_review_ids, only_meta=False):
        """
        解析亚马逊评论HTML，提取关键信息（适配真实标签结构）
        """
        asin = task["asin"] if 'new_asin' not in task.keys() else task["new_asin"]

        try:
            html_content = resp['resp']
            # 解析逻辑尽量保持纯 Python 路径，避免高频强制 gc 导致不可控抖动
            if '/ajax/reviews/' in resp['url']:
                # if resp['resp'].find(asin) == -1:0
                #     print('不是该商品的数据')
                #     raise Exception('不是该商品的评论数据')
                array_str_list = resp['resp'].split('&&&')
                html_content = ''
                for review_html in array_str_list:
                    if ('a-unordered-list a-nostyle a-vertical' in review_html or
                            ('cr-filter-info-review-rating-count' in review_html and 'page' in resp.keys())
                            or 'show-more-button' in review_html
                    ):
                        html_content = html_content + "\n" + json.loads(review_html)[2]
                soup = BeautifulSoup(html_content, "html.parser")
                del array_str_list  # 释放临时变量内存
            else:
                soup = BeautifulSoup(html_content, "html.parser")
                cr_state_span = soup.find('span', id='cr-state-object')
                if not cr_state_span or not cr_state_span.get('data-state'):
                    print("⚠️  未找到cr-state-object标签或data-state属性")
                    raise Exception("⚠️  未找到cr-state-object标签或data-state属性")
                else:
                    data_state = json.loads(cr_state_span['data-state'])
                    self.reviews_csrf_token = data_state.get('reviewsCsrfToken')
            next_page_token = self._extract_next_page_token(soup)  #获取token
            if 'page' in resp.keys():
                count_ = soup.find('div', {'data-hook': 'cr-filter-info-review-rating-count'}).get_text(strip=True)
                # print(count_)

                if re.findall(r'(\d{1,3}(?:[,.]\d{3})*)', count_):
                    self.review_counts = int(
                        re.findall(r'(\d{1,3}(?:[,.]\d{3})*)', count_)[0].replace(',', '').replace('.', ''))
                    logger.info(f"{asin}-评论数 ：{self.review_counts}--数据包大小{len(resp['resp'])}")
                    self.pages = 10 if self.review_counts > 100 else math.ceil(self.review_counts / 10)
                    self.pages = min(task.get('max_pages'), self.pages) if task.get('max_pages') else self.pages

                else:
                    print(f'没有评论：{count_}-{self.task}')
                    self.review_counts = 0
                    self.pages = 1
                    return [], next_page_token

            if only_meta:
                return [], next_page_token

        except Exception as e:
            raise
        reviews = []
        # 兼容两种评论块标签（亚马逊不同页面可能有差异）
        _blocks = soup.find_all('div', class_='a-section review aok-relative') or \
                  soup.find_all('li', {'data-hook': 'review'})
        # 将每个 block 转为独立 HTML 字符串，释放主 soup 树（避免大树在循环中导致 SIGSEGV）
        block_htmls = [str(b) for b in _blocks]
        num_blocks = len(block_htmls)
        # print(f'num_blocks:{num_blocks}')
        del _blocks
        soup.decompose()
        del soup
        duplicate_count = 0
        invalid_count = 0
        for idx in range(1, num_blocks + 1):
            # print(f'idx{idx}')
            block_html = block_htmls[idx - 1]
            review_data = {}
            missing_fields = []
            try:
                review_data, missing_fields = parse_review_block_html(
                    block_html,
                    task,
                    SITE_MAPPING,
                    self.get_site,
                    logger,
                )
                if not review_data:
                    continue
                if review_data['reviewId'] in seen_review_ids:
                    duplicate_count += 1
                    continue
                seen_review_ids.add(review_data['reviewId'])

                if not missing_fields:
                    reviews.append(review_data)
                else:
                    invalid_count += 1
                    raise Exception(f"数据解析异常,不完整: {','.join(missing_fields)}")
            except Exception as e:
                try:
                    print(e)
                    alert_review_parse_error(
                        task=task,
                        review_data=review_data,
                        missing_fields=missing_fields,
                        block_html=block_html,
                        error_msg=str(e),
                        source="curl",
                        log=logger,
                    )
                    # 使用 MySQL 存储异常数据
                    from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
                    mysql_db = MySQLTaskDB()
                    # resp['resp'] = block_html
                    mysql_db.insert_reviews_error(
                        asin=task['asin'],
                        country=task.get('country', ''),
                        resp=json.dumps(resp, ensure_ascii=False),
                        review_data=review_data,
                        task_info=task,
                        error_msg=str(e)
                    )
                    mysql_db.close()
                except Exception as e:
                    logger.error(f"告警/存储异常：{str(e)}")

                continue

        self._record_review_page_stats(resp, num_blocks, len(reviews), duplicate_count, invalid_count)
        return reviews, next_page_token

    # def on_request(self, page, task, review_counts):
    #     """监听请求，当请求成功后解析并打印评论数据"""
    #     idx = 1
    #     print("\n" + "-" * 60)
    #     print("📝 开始评论数据解析")
    #     res_list = page.listen.steps(timeout=5)
    #     all_parsed_comments = []  # 收集所有解析后的评论数据
    #
    #     if not res_list:
    #         return None
    #
    #     print("-" * 60)
    #     seen_review_ids = set()
    #     for request in res_list:
    #         if request:
    #             # print(f"✅ 获取到数据包: {request.url}")
    #             resp = request.response.body
    #             # 解析并打印评论数据，并收集解析后的数据
    #             if resp:
    #                 reviews = self.parse_reviews_ajax({'url': request.url, 'resp': resp}, task, seen_review_ids)
    #                 all_parsed_comments.extend(reviews)
    #         # else:
    #         #     # 检查是否有评论按钮但无评论
    #         #     comment_ele = page.ele('@class=reds-icon')
    #         #     if comment_ele and comment_ele.text == '评论':
    #         #         print("📝 该笔记暂无评论")
    #         #         return []
    #         #     else:
    #         #         print("⚠️  未获取到评论数据包")
    #         #         return None
    #
    #     print("\n" + "-" * 60)
    #
    #     if (len(all_parsed_comments) < 100 and len(all_parsed_comments) < review_counts):
    #         logger.warning(f'数据有丢失{len(all_parsed_comments)}--{review_counts}')
    #
    #     return all_parsed_comments

    # @retry(stop_max_attempt_number=2, wait_random_min=1000, wait_random_max=2000, )
    # def get_reviews_ajax_more(self, task, pages):
    #     for i in range(2, pages + 1):
    #         time.sleep(random.randint(3, 5))
    #         next_page = self.page.ele('@class=a-last', timeout=2)
    #         if not next_page:
    #             print('@class=a-last not find ')
    #             time.sleep(random.randint(3, 5))
    #             next_page = self.page.ele('@class=a-last', timeout=2)
    #             # self.page.refresh()
    #             # raise
    #         url = next_page.ele('a').attr('href')
    #         print(f'------获取{task["asin"]} 第{i}页------{url}')
    #         page_num = re.findall(r'pageNumber=(\d+)', url)[0]
    #         if str(page_num) != str(i):
    #             # self.page.refresh()
    #             # raise Exception('翻页失败')
    #             print('翻页失败')
    #             time.sleep(1)
    #             next_page = self.page.ele('@class=a-last', timeout=2)
    #             next_page.ele('a').click()
    #             time.sleep(random.randint(3, 5))
    #             # next_page = self.page.ele('@class=a-last', timeout=2)
    #             # time.sleep(random.randint(3, 5))
    #         else:
    #             next_page.ele('a').click()
    #
    #         time.sleep(1)
    #     print('翻页完成')
    #     return True
    # #
    # def get_reviews_ajax(self, task):  # 用监听的方式获取数据
    #     try:
    #         url = f'https://{SITE_MAPPING[task["country"]]}/product-reviews/{task["asin"]}'
    #         all_datas = []
    #         self.init_dp(self.account_info.fingerprint_id)
    #         self.page.listen.set_targets()
    #         # 监听子评论
    #         self.page.listen.start([
    #             f'https://www.amazon.com/product-reviews/{task["asin"]}',
    #             f'https://www.amazon.com/portal/customer-reviews/ajax/reviews/get/ref=']
    #         )
    #         self.page.get(url, timeout=10)
    #         time.sleep(1)
    #         self.verify_login(url)
    #         # 点击查看更多评论进入评论页面
    #         result, datas = self.verify_asin_is_error(task)
    #         if result == False:
    #             return datas
    #         if self.page.ele('@data-hook=cr-filter-info-review-rating-count', timeout=2):
    #             counts = self.page.ele('@data-hook=cr-filter-info-review-rating-count').text
    #             print(counts)
    #
    #             if 'To see more reviews' in counts:
    #                 logger.error(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #                 raise Exception(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #             if re.findall(r'(\d{1,3}(?:,\d{3})*)\s*customer reviews', counts):
    #                 review_counts = int(
    #                     re.findall(r'(\d{1,3}(?:,\d{3})*)\s*customer reviews', counts)[0].replace(',', ''))
    #         elif self.page.ele('@data-hook=request-more-reviews-widget', timeout=2):
    #             text = self.page.ele('@data-hook=request-more-reviews-widget').text
    #             if 'To see more reviews' in text or 'Your request to see more reviews has been sent' in text:
    #                 logger.error(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #                 raise Exception(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #         pages = 10 if review_counts > 100 else math.ceil(review_counts / 10)
    #         self.get_reviews_ajax_more(task, pages)
    #         all_datas = self.on_request(self.page, task, review_counts)
    #         self.page.close()  # 关闭标签页 断开监听
    #         return all_datas  # 任务完成
    #     except:
    #
    #         logger.error(f'任务失败{task}-{self.account_info.username}{traceback.format_exc()}')
    #         raise Exception(f'任务失败{task}-{self.account_info.username}{traceback.format_exc()}')
    #
    #     # finally:
    #     #
    #     #     try:
    #     #         if self.page:
    #     #             self.page.close()  # 关闭标签页
    #     #             self.chrome.quit_fingerprint(self.account_info.fingerprint_id)
    #     #     except:
    #     #         ...
    #     #     ...

    # @retry(stop_max_attempt_number=2, wait_random_min=1000, wait_random_max=2000, )
    # def get_counts(self):
    #     if self.page.ele('@data-hook=cr-filter-info-review-rating-count', timeout=2):
    #         counts = self.page.ele('@data-hook=cr-filter-info-review-rating-count').text
    #         print(counts)
    #         if 'No customer reviews' in counts:
    #             # print('No customer reviews')
    #             return 0
    #         if 'To see more reviews' in counts:
    #             logger.error(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #             raise Exception(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #         if re.findall(r'(\d{1,3}(?:,\d{3})*)\s*customer reviews', counts):
    #             self.review_counts = int(
    #                 re.findall(r'(\d{1,3}(?:,\d{3})*)\s*customer reviews', counts)[0].replace(',', ''))
    #
    #     elif self.page.ele('@data-hook=request-more-reviews-widget', timeout=2):
    #         if 'To see more reviews' in self.page.ele('@data-hook=request-more-reviews-widget').text:
    #             logger.error(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #             raise Exception(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
    #
    #     if self.review_counts == None:
    #         time.sleep(5)
    #         raise Exception('网页加载失败')
    #     return self.review_counts

    @retry(stop_max_attempt_number=2, wait_random_min=1000, wait_random_max=2000, )
    def get_first_page(self, task, filter_star='', is_exists_type = False):
        is_exists_type = bool(is_exists_type or should_use_current_format_filter(task))
        use_recent_sort = should_use_recent_sort(task, getattr(self, "date_cutoff", None))


        fp = self.user_agent
        headers = self.generate_sec_ch_headers()
        #https://www.amazon.com/product-reviews/B0F7Y2GZL3/ref=cm_cr_arp_d_viewopt_srt?sortBy=recent&pageNumber=1
        #https://www.amazon.com/product-reviews/B00TWTC5G2/ref=cm_cr_arp_d_viewopt_fmt?ie=UTF8&reviewerType=all_reviews&formatType=current_format&pageNumber=1
        # https://www.amazon.com/product-reviews/B00TWTC5G2/ref=cm_cr_arp_d_viewopt_srt?sortBy=recent&pageNumber=1
        # https://www.amazon.com/product-reviews/B0F7Y2GZL3/ref=cm_cr_arp_d_viewopt_sr?filterByStar=five_star&pageNumber=1
        asin = task["asin"] if 'new_asin' not in task.keys() else task["new_asin"]

        # 构造 URL，支持星级过滤
        if filter_star:
            url = f"https://{SITE_MAPPING[task['country'].upper()]}/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_sr?filterByStar={filter_star}&pageNumber=1"
        else:
            url = f"https://{SITE_MAPPING[task['country'].upper()]}/product-reviews/{asin}/ref=cm_cr_arp_d_viewopt_srt?pageNumber=1"
        url = add_recent_sort_param(url, use_recent_sort)
        url = add_current_format_param(url, is_exists_type)

        self.first_page_url = url

        response = self.request('get', url, headers=headers,
                                proxies=self.proxies, verify=False,
                                    )


        if 'cr-state-object' in response.text:
            # 首页获取成功事件
            try:
                _redis = self._get_redis()
                if _redis:
                    push_event(
                        _redis, EventType.PAGE_FETCHED,
                        username=getattr(self.account_info, 'username', '') if self.account_info else '',
                        asin=asin,
                        country=task.get('country', ''),
                        page=1,
                        http_status=response.status_code,
                    )
            except Exception:
                pass
            return response.text
        elif 'find that page. Try searching or go to Amazon\'s home page' in response.text or 'a href="/ref=cs_404_link"' in response.text or response.status_code == 404:
            print('地址错误')
            url1 = f'https://{SITE_MAPPING[task["country"].upper()]}/dp/{asin}'
            response1 = self.request('get', f'https://{SITE_MAPPING[task["country"].upper()]}/dp/{asin}', headers=headers,
                                     proxies=self.proxies,verify=False)
            if response1.status_code == 404:
                logger.error(f'ASIN无效(商品页404): {url1}')
                self.review_counts = 0
                self._asin_not_found = True
                return None
            else:
                # 如果被重定向到登录页或验证码页，不能判断为 ASIN 无效
                if 'ap/signin' in response1.url or 'validateCaptcha' in response1.url:
                    raise Exception(f'访问商品页被重定向到登录/验证码，账号异常: {response1.url}')
                pattern = r'id="averageCustomerReviews" data-asin="([^"]+)"'
                # 从页面属性中提取 ASIN，例如 data-csa-c-asin="B0F1DRJP4V"
                matches = re.findall(pattern, response1.text)
                if not matches:
                    if 'id="title_feature_div"' not in response1.text:
                        logger.error(f'ASIN无效(商品页无标题/评论区): {url1}')
                        send_custom_robot_group_message(f'地址错误2；{url1} - {self.task}')
                        self.review_counts = 0
                        self._asin_not_found = True
                        return None
                    else:
                        raise Exception('未找到 averageCustomerReviews，无法识别新asin')
                current_asin = matches[0]
                if current_asin != task["asin"]:   #B099FPD3H3
                    logger.info(f'ASIN重定向: {task["asin"]} → {current_asin}')
                    task['new_asin'] = current_asin
                    raise Exception(f'有新asin{current_asin}')
                    # send_custom_robot_group_message(f'地址错误；{url1} - {self.task}')
                    # return None
                else:
                    raise Exception('重新请求主页地址')
        else:
            # 首页获取失败事件
            try:
                _redis = self._get_redis()
                if _redis:
                    push_event(
                        _redis, EventType.PAGE_FAILED,
                        username=getattr(self.account_info, 'username', '') if self.account_info else '',
                        asin=asin,
                        country=task.get('country', ''),
                        page=1,
                        http_status=response.status_code,
                        error_msg='首页获取失败: 无 cr-state-object',
                    )
            except Exception:
                pass
            raise Exception('首页获取失败')

    def submit_request_more_reviews(self, first_page, task):  #账号评论获取被限制先发个申请
        soup = BeautifulSoup(first_page, "html.parser")

        request_widget = soup.find('span', class_='cr-request', attrs={'data-hook': 'request-more-reviews-widget'})
        if not request_widget:
            return None

        declarative = request_widget.find('span', class_='a-declarative', attrs={'data-action': 'reviews:ajax-post'})
        if not declarative:
            if 'Your request to see more reviews has been sent' in first_page:
                logger.info(f'request-more-reviews 已经提交完成: asin={task["asin"]}')
            return None

        ajax_post_raw = declarative.get('data-reviews:ajax-post') or declarative.get('data-reviews\:ajax-post')
        if not ajax_post_raw:
            return None

        ajax_post = json.loads(ajax_post_raw)
        params = ajax_post.get('params') or {}
        csrf_t = params.get('csrfT')
        asin = params.get('asin') or task.get('asin')
        submit_path = ajax_post.get('url')
        if not csrf_t or not asin or not submit_path:
            return None

        submit_url = f"https://{SITE_MAPPING[task['country'].upper()]}{submit_path}"
        headers = {
            "accept": "text/html,*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "device-memory": "8",
            "dnt": "1",
            "downlink": "1.55",
            "dpr": "2",
            "ect": "3g",
            "origin": f"https://{SITE_MAPPING[task['country'].upper()]}",
            "priority": "u=1, i",
            "referer": self.first_page_url,
            "rtt": "1050",
            "sec-ch-device-memory": "8",
            "sec-ch-dpr": "2",
            "sec-ch-ua": "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
            "sec-ch-ua-full-version-list": "\"Google Chrome\";v=\"131.0.6778.109\", \"Chromium\";v=\"131.0.6778.109\", \"Not_A Brand\";v=\"24.0.0.0\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"Windows\"",
            "sec-ch-ua-platform-version": "\"10.0.0\"",
            "sec-ch-viewport-width": "405",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": self.user_agent,
            "viewport-width": "405",
            "x-amzn-flow-closure-id": "1775182169",
            "x-requested-with": "XMLHttpRequest"
        }
        headers.update(self.generate_sec_ch_headers())
        data = {
            "csrfT": csrf_t,
            "asin": asin,
            "scope": "reviewsAjax0"
        }
        response = self.request(
            'post',
            url=submit_url,
            headers=headers,
            proxies=self.proxies,
            data=data,
            timeout=25,
            verify=False,
        )
        logger.info(f"request-more-reviews 提交完成: asin={asin}, status={response.status_code} resp={response.text}")
        return response

    def init_(self,url):
        #做初始化

        time_str = self.account_info.refresh_time if self.account_info.refresh_time else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        refresh_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
        current_time = datetime.now()
        # 在 REFRESH_TIME 基础上加入 0~10 小时随机抖动，避免账号集中在同一时刻重新登录
        refresh_jitter_hours = random.uniform(0, 10)
        effective_refresh_hours = max(1, REFRESH_TIME - refresh_jitter_hours)
        if self.account_info.user_agent and type(self.account_info.cookies) == dict  and  current_time - refresh_time < timedelta(hours=effective_refresh_hours): #
            #直接拿数据库里的cookies
            self._log.info('从数据库缓存获取cookies')
            self.cookies = self.account_info.cookies
            self.inject_cookies_to_session(self.cookies,domain=SITE_MAPPING[self.task["country"].upper()].replace('www',''))
            self.user_agent = json.loads(self.account_info.user_agent)
            self.proxies = self.account_info.proxy_
        else:
            if self.use_local_browser:
                self.get_info_from_local_dp(url)
            else:
                self.get_info_from_dp(url)

    def get_counts_(self, first_page, parse_first_page=False, seen_review_ids=None, filter_star=''):
        self.review_counts = 0
        if 'request-more-reviews-widget' in first_page:
            try:
                self.submit_request_more_reviews(first_page, self.task)
            except Exception:
                logger.error(f'提交 request-more-reviews 失败: {traceback.format_exc()}')
            logger.error(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')
            raise Exception(f'账号无法查看评论，可能被封，请查看{self.account_info.username}')

        if 'cr-filter-info-review-rating-count' not in first_page:
            raise Exception('评论数匹配失败')

        # 复用 parse_reviews_ajax 内已有逻辑：初始化 csrf/token + 解析 review_counts/pages
        effective_seen_review_ids = seen_review_ids if seen_review_ids is not None else set()
        reviews, next_page_token = self.parse_reviews_ajax(
            {"resp": first_page, 'url': 'get_counts_bootstrap', 'page': 1, 'filter_star': filter_star},
            self.task,
            effective_seen_review_ids,
            parse_first_page
        )

        if self.review_counts == 0:
            logger.info(f'没有评论：{self.task}-数据包大小{len(first_page)}')
            return [], next_page_token

        logger.info((f'评论条数:{self.review_counts} --{self.task["asin"]}-数据包大小{len(first_page)}'))
        return reviews, next_page_token


    @staticmethod
    def _parse_date_cutoff(query_conditions):
        """解析 date_from，支持相对天数 '30d' 和绝对日期 '2025-04-01'"""
        date_from = (query_conditions or {}).get('date_from', '')
        if not date_from:
            return None
        try:
            s = str(date_from).strip()
            if s.endswith('d'):
                days = int(s[:-1])
                return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
            return datetime.strptime(s, '%Y-%m-%d')
        except Exception:
            logger.warning(f"[日期截止] date_from 格式无法解析: {date_from!r}，忽略")
            return None

    def get_reviews_begin(self, task):
        try:
            self._asin_not_found = False
            self.page_token = None
            task['asin'] = task['asin'].replace('\u200e', '')
            self.task = task
            url = f'https://{SITE_MAPPING[task["country"].upper()]}/product-reviews/{task["asin"]}'
            all_datas = []
            self._reset_review_integrity_stats()

            if not self.cookies:  # 做初始化，获取cookies代理等信息
                self.init_(url)

            # 检查是否需要按星级过滤
            star_filters = []
            original_stars = []
            if task.get('query_conditions') and task['query_conditions'].get('stars'):
                # stars 可以是单个数字或列表，如 5 或 [5, 4, 3]
                stars = task['query_conditions']['stars']
                if isinstance(stars, int):
                    stars = [stars]
                original_stars = [s for s in stars if s in [1, 2, 3, 4, 5]]
                star_filters = [self.get_star_filter_value(s) for s in original_stars if s]
                self._log.info(f"[过滤条件] 按星级过滤：{stars} -> {star_filters}")
            
            # 如果没有星级过滤，使用空字符串（获取所有评论）
            if not star_filters:
                star_filters = ['']

            # 日期截止条件（date_from）：设置后跳过完整性校验，按时间排序时遇到旧评论即停止翻页
            date_cutoff = self._parse_date_cutoff(task.get('query_conditions'))
            if date_cutoff:
                self._log.info(f"[日期截止] 仅获取 {date_cutoff.strftime('%Y-%m-%d')} 之后的评论")
            self.date_cutoff = date_cutoff
            date_cutoff_alert_sent = False

            def _last_review_date(rows: list) -> str:
                for review in reversed(rows or []):
                    review_date = (review or {}).get('reviewDate')
                    if review_date:
                        return review_date
                return ""

            def _date_cutoff_gap_days(review_date: str):
                if not date_cutoff or not review_date:
                    return None
                try:
                    return (datetime.strptime(review_date, '%Y-%m-%d') - date_cutoff).days
                except Exception:
                    return None

            def _send_date_cutoff_alert(
                    pages_seen: int,
                    star_label: str,
                    collected_count: int,
                    last_review_date: str = "",
                    allow_short: bool = False,
                    alert_reason: str = "",
            ) -> None:
                nonlocal date_cutoff_alert_sent
                if not date_cutoff or (pages_seen < 10 and not allow_short) or date_cutoff_alert_sent:
                    return
                date_cutoff_alert_sent = True
                reason = alert_reason or (
                    f"评论任务抓到第{pages_seen}页仍未到达 date_from，可能超过单条件100条上限，请人工处理。"
                )
                message = (
                    f"[日期截止告警] {reason}ASIN={task.get('asin', '')}, country={task.get('country', '')}, "
                    f"date_from={date_cutoff.strftime('%Y-%m-%d')}, star={star_label}, "
                    f"最后评论日期={last_review_date or '-'}, 已抓取={collected_count}"
                )
                self._log.warning(message)
                # try:
                #     send_custom_robot_group_message(message,)
                # except Exception:
                #     self._log.warning(f"[日期截止告警] 发送失败: {traceback.format_exc()[:500]}")

            def _validate_date_cutoff_short_page_completion(
                    pages_seen: int,
                    expected_pages: int,
                    star_label: str,
                    collected_count: int,
                    expected_count: int,
                    last_review_date: str = "",
            ) -> None:
                if not date_cutoff or pages_seen >= 10:
                    return
                if expected_pages and pages_seen < expected_pages:
                    gap_days = _date_cutoff_gap_days(last_review_date)
                    if gap_days is not None and abs(gap_days) <= 5:
                        _send_date_cutoff_alert(
                            pages_seen,
                            star_label,
                            collected_count,
                            last_review_date,
                            allow_short=True,
                            alert_reason=(
                                f"评论任务第{pages_seen}页提前结束，但最小评论日期距 date_from "
                                f"{abs(gap_days)}天以内，未触发重试，请人工确认。"
                            ),
                        )
                        return
                    raise Exception(
                        f"[日期截止完整性] 翻页提前结束，触发重试。ASIN={task.get('asin', '')}, "
                        f"country={task.get('country', '')}, date_from={date_cutoff.strftime('%Y-%m-%d')}, "
                        f"star={star_label}, 已抓到第{pages_seen}页, 预计={expected_pages}页, "
                        f"最后评论日期={last_review_date or '-'}, 已抓取={collected_count}"
                    )
                if not expected_pages or expected_pages >= 10 or expected_count <= 0:
                    return
                if collected_count != expected_count:
                    self._log.warning(
                        f"[日期截止完整性] {star_label}星 实际={collected_count}, 预期={expected_count}, "
                        f"最后评论日期={last_review_date or '-'}"
                    )
                if collected_count < 1 or (
                        collected_count / expected_count < 0.95 and expected_count - collected_count > 10
                ):
                    raise Exception(
                        f"[日期截止完整性] 评论抓取数量不足，触发重试。ASIN={task.get('asin', '')}, "
                        f"country={task.get('country', '')}, date_from={date_cutoff.strftime('%Y-%m-%d')}, "
                        f"star={star_label}, 实际={collected_count}, 预期={expected_count}, "
                        f"最后评论日期={last_review_date or '-'}"
                    )

            seen_review_ids = set()
            total_expected_reviews = 0
            # 默认只抓当前变体；显式 all_variants=true 才抓全部变体
            is_exists_type = should_use_current_format_filter(task)
            cached_first_page_datas = None
            cached_next_page_token = None
            cached_first_page_ready = False  #True 表示已经获取了第一页的数据 后续可以复用
            
            # 优化：如果star_filters包含全部星级(1-5)，先不按星级过滤直接获取评论数
            should_check_total_first = set(original_stars) == {1, 2, 3, 4, 5}

            if should_check_total_first:
                self._log.info("[抓取策略] 检测到全星级任务，先获取总评论数")
                # 首页已经按 all_variants 参数决定是否带 formatType=current_format
                first_page_check = self.get_first_page(task, '', is_exists_type)
                if first_page_check:
                    cached_first_page_datas, cached_next_page_token = self.get_counts_(
                        first_page_check,
                        parse_first_page=False,
                        seen_review_ids=seen_review_ids
                    )
                    total_reviews = self.review_counts


                    scope = "当前变体" if is_exists_type else "全部变体"
                    self._log.info(f"[任务信息] 总评论数={total_reviews}，范围={scope}")
                    if total_reviews == 0:
                        return []
                    if total_reviews <= 100:
                        self._log.info(f"[抓取策略] 评论数{total_reviews}<=100，直接全量抓取")
                        star_filters = ['']
                        cached_first_page_ready = True
                    else:
                        self._log.info(f"[抓取策略] 评论数{total_reviews}>100，按星级逐个筛选")
                        # 保持原有的星级过滤列表
                else:  #说明地址错误
                    return []
            # 循环处理每个星级
            for filter_star in star_filters:
                next_page_token = None
                star_label = filter_star.replace('_star', '') if isinstance(filter_star, str) and filter_star else 'all'
                if star_label is None:
                    star_label = 'all'
                star_label = str(star_label)
                is_numeric_star_label = star_label.isdigit()
                filter_date_cutoff_hit = False
                last_page_seen = 1
                last_date_seen = ""
                star_start_count = len(all_datas)
                self._log.info(f"[星级切换] 开始抓取 {star_label} 星")

                # 获取当前星级的第一页（只有filter_star为空时才能复用缓存）
                # 针对有星级筛选的时候已经获取到了第一页的数据的情况
                if cached_first_page_ready and filter_star == '':
                    datas = cached_first_page_datas or []
                    next_page_token = cached_next_page_token
                    cached_first_page_ready = False
                    self.pages = 10 if self.review_counts > 100 else math.ceil(self.review_counts / 10)
                    self.pages = min(task.get('max_pages'), self.pages) if task.get('max_pages') else self.pages
                    page = 2
                    first_page = None
                elif self.reviews_csrf_token: #(星级筛选数量大于100只能按星级逐个筛选的情况，由于已经获取到了self.reviews_csrf_token，就不用get_first_page可以省流量)
                    page = 1
                    self.pages = 10
                else: #第一次获取首页
                    first_page = self.get_first_page(task, filter_star, is_exists_type)
                    if first_page == None: #地址错误
                        return []
                    # URL/AJAX 已经按 all_variants 决定评论范围，不再依赖页面下拉框判断
                    datas, next_page_token = self.get_counts_(
                        first_page,
                        parse_first_page=False,
                        seen_review_ids=seen_review_ids,
                        filter_star=filter_star,
                    )
                    self.pages = 10 if self.review_counts > 100 else math.ceil(self.review_counts / 10)
                    self.pages = min(task.get('max_pages'), self.pages) if task.get('max_pages') else self.pages
                    page = 2
                    if self.review_counts == 0:
                        self._log.info(f"[星级切换] {star_label}星 无评论，跳过")
                        continue

                if page == 1:
                    next_page_token = None
                    self._log.info(f"[翻页] {star_label}星 有变体={is_exists_type}，获取总评论数中")
                else:
                    self._log.info(f"[翻页] {star_label}星 有变体={is_exists_type}，总评论数={self.review_counts}，预计翻页={self.pages}页，hasToken={next_page_token is not None}")

                if page == 2:
                    if date_cutoff and datas:
                        last_date_str = datas[-1].get('reviewDate')
                        last_date_seen = last_date_str or last_date_seen
                        try:
                            if last_date_str and datetime.strptime(last_date_str, '%Y-%m-%d') < date_cutoff:
                                datas = [r for r in datas if r.get('reviewDate') and datetime.strptime(r['reviewDate'], '%Y-%m-%d') >= date_cutoff]
                                filter_date_cutoff_hit = True
                                self._log.info(f"[日期截止] {star_label}星 第1页末尾={last_date_str}，停止翻页")
                                page = self.pages + 1  # 跳过 while 循环
                        except Exception:
                            pass
                    all_datas.extend(datas)
                    self._log.info(f"[翻页] {star_label}星 第1页解析={len(datas)}条")

                error_page = 3  #避免死循环
                # 抓取后续页
                while page <= self.pages:
                    try:
                        if  next_page_token and int(next_page_token.get('pageNumber',page)) != page:
                            page = int(next_page_token['pageNumber'])
                            self._log.warning(f"[翻页] page不一致，修正为{page}，token={next_page_token}")
                            error_page = error_page -1
                            if error_page <0 :
                                self._log.error(f"[翻页] 错误page数超过3次，停止")
                                break
                        returned_token = self.get_more_reviews(
                            task, page, seen_review_ids, all_datas,
                            is_exists_type, filter_star, next_page_token
                        )
                        last_page_seen = page
                        last_date_seen = _last_review_date(all_datas[star_start_count:]) or last_date_seen
                        # 如果返回了 token，说明使用了新分页方式
                        if returned_token:
                            self.page_token = copy.deepcopy(next_page_token) #保存上一页的token
                            next_page_token = returned_token
                        elif (next_page_token and not returned_token) and page < self.pages:
                            self._log.error(f"[翻页] {star_label}星 第{page}页 token丢失，总页数={self.pages}")

                        # 日期截止检查：末尾评论早于截止日期则过滤并停止翻页
                        if date_cutoff and all_datas:
                            last_date_str = all_datas[-1].get('reviewDate')
                            try:
                                if last_date_str and datetime.strptime(last_date_str, '%Y-%m-%d') < date_cutoff:
                                    all_datas[:] = [r for r in all_datas if r.get('reviewDate') and datetime.strptime(r['reviewDate'], '%Y-%m-%d') >= date_cutoff]
                                    filter_date_cutoff_hit = True
                                    self._log.info(f"[日期截止] {star_label}星 第{page}页末尾={last_date_str}，停止翻页")
                                    break
                            except Exception:
                                pass

                    except Exception as e:
                        self._log.error(f"[翻页] {star_label}星 第{page}页请求失败: {str(e)}")
                        raise
                    page = page + 1

                if date_cutoff and not filter_date_cutoff_hit:
                    expected_pages = int(self.pages or last_page_seen or 0)
                    current_star_count = max(0, len(all_datas) - star_start_count)
                    expected_count = min(int(self.review_counts or 0), expected_pages * 10) if expected_pages else 0
                    _validate_date_cutoff_short_page_completion(
                        last_page_seen,
                        expected_pages,
                        star_label,
                        current_star_count,
                        expected_count,
                        last_date_seen,
                    )
                    _send_date_cutoff_alert(last_page_seen, star_label, len(all_datas), last_date_seen)

                # 设置了日期截止时不做完整性校验（预期数基于全量评论数，实际只取一段无意义）
                if not date_cutoff:
                    self._record_review_page_completion(last_page_seen, self.pages)
                    total_expected_reviews += min(self.review_counts, self.pages * 10)

                current_star_count = len([r for r in all_datas if (is_numeric_star_label and r.get('rating') == int(star_label)) or not is_numeric_star_label])
                self._log.info(f"[星级完成] {star_label}星 抓取完毕，本星级={current_star_count}条，累计={len(all_datas)}条")

                if filter_star and filter_star != star_filters[-1]:
                    _pass_delay = random.uniform(STAR_PASS_DELAY_MIN, STAR_PASS_DELAY_MAX)
                    self._log.info(f"[星级切换] 延迟 {_pass_delay:.1f}s")
                    time.sleep(_pass_delay)

            if not date_cutoff:
                actual_count = len(all_datas)
                raw_slots = int(getattr(self, '_review_raw_slots_count', 0) or 0)
                duplicate_slots = int(getattr(self, '_review_duplicate_slots_count', 0) or 0)
                invalid_slots = int(getattr(self, '_review_invalid_slots_count', 0) or 0)
                pages_complete = (
                    bool(getattr(self, '_review_page_completion_ok', False))
                    and int(getattr(self, '_review_expected_pages_total', 0) or 0) > 0
                    and int(getattr(self, '_review_expected_pages_seen', 0) or 0) >= int(getattr(self, '_review_expected_pages_total', 0) or 0)
                )

                if total_expected_reviews > 0 and actual_count != total_expected_reviews:
                    self._log.warning(
                        f"[完整性] 实际={actual_count}, 预期={total_expected_reviews}, "
                        f"raw_blocks={raw_slots}, duplicate={duplicate_slots}, invalid={invalid_slots}, "
                        f"pages={getattr(self, '_review_expected_pages_seen', 0)}/"
                        f"{getattr(self, '_review_expected_pages_total', 0)}"
                    )

                if total_expected_reviews and (actual_count / total_expected_reviews) * 100 < 95:
                    diff = total_expected_reviews - actual_count
                    if pages_complete and raw_slots > 0 and raw_slots < total_expected_reviews and invalid_slots == 0:
                        self._log.warning(
                            f"[完整性] 已走完分页，但 Amazon 实际返回评论块少于展示数量，按实际可获取数据完成。"
                            f"实际={actual_count}, 预期={total_expected_reviews}, raw_blocks={raw_slots}, "
                            f"duplicate={duplicate_slots}, invalid={invalid_slots}, task={task}"
                        )
                    elif raw_slots > 0 and raw_slots / total_expected_reviews >= 0.95 and duplicate_slots >= diff:
                        self._log.warning(
                            f"[完整性] 唯一评论数不足但原始评论槽位已达标，判定为重复评论导致。"
                            f"实际={actual_count}, 预期={total_expected_reviews}, raw_blocks={raw_slots}, "
                            f"duplicate={duplicate_slots}, invalid={invalid_slots}, task={task}"
                        )
                    elif diff > 10 or actual_count < 1:
                        raise Exception(
                            f'评论抓取数量不足，触发重试：实际={actual_count}, 预期={total_expected_reviews}, 差值={diff}'
                        )
                    else:
                        send_custom_robot_group_message(
                            f'数据有部分丢失,差异不大{actual_count}--{total_expected_reviews}--{task}',
                            at_mobiles=['17398238551']
                        )


            # 更新cookies
            if self.use_curl_session:
                try:
                    self.account_info.cookies = self.curl_cookie_to_requests_dict(self.curl_session)
                except:
                    self.account_info.cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
            else:
                self.account_info.cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
            try:
                self._log.info(f"[任务完成] 共抓取={len(all_datas)}条，预期={total_expected_reviews}条")
            except Exception:
                pass
            return all_datas
        except (CookieRefreshExhaustedException, AccountSwitchRequiredException):
            raise  # 保留原始类型，让 get_reviews_main 正确识别
        except Exception:
            username_ = getattr(self.account_info, 'username', 'N/A')
            raise Exception(f'任务失败{task}-{username_}{traceback.format_exc()}')


    def _record_usage_log(self, task, start_time, success, review_count=0,
                          retry_count=0, error_msg="", worker_id=None):
        try:
            end_time = datetime.now()
            db = MySQLTaskDB()
            db.insert_usage_log(
                task_id=str(task.get('task_id', '')) if str(task.get('task_id', '')) else str(task.get('id', '')),
                asin=task.get('asin', ''),
                country=task.get('country', ''),
                username=getattr(self.account_info, 'username', ''),
                success=success,
                review_count=review_count,
                start_time=start_time.strftime('%Y-%m-%d %H:%M:%S'),
                end_time=end_time.strftime('%Y-%m-%d %H:%M:%S'),
                duration_seconds=int((end_time - start_time).total_seconds()),
                retry_count=retry_count,
                error_msg=str(error_msg)[:2000] if error_msg else '',
                worker_id=str(worker_id or ''),
                task_type=task.get('task_type', 'review'),
            )
            db.close()
        except Exception:
            logger.warning(f"insert_usage_log failed: {traceback.format_exc()}")


    def _get_redis(self):
        """懒加载 Redis 客户端（用于事件日志）"""
        if not hasattr(self, '_redis_client') or self._redis_client is None:
            try:
                import redis as redis_lib
                from app.crawlers.amazon_crawler.shuler.util.config import (
                    REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_USERNAME, REDIS_DB,
                )
                self._redis_client = redis_lib.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    username=REDIS_USERNAME, password=REDIS_PASSWORD,
                    db=REDIS_DB, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
            except Exception:
                self._redis_client = None
        return self._redis_client

    def get_reviews_main(self, task, worker_id=None, account_manager=None):
        """带重试的抓取入口，重试逻辑下沉到 reviews 内。"""
        self._proxy_ip_cache: str = ''
        retry_left = RETRY_TIMES
        last_error = None
        success = False
        self.worker_id = worker_id
        _log_start_time = datetime.now()
        self.reviews_csrf_token = ''
        asin = task.get('asin', '')
        country = task.get('country', '')
        username = getattr(self.account_info, 'username', '')
        _redis = self._get_redis()
        try:
            real_ip = self._resolve_proxy_ip()
        except Exception:
            real_ip = ''

        # 绑定上下文到 loguru（便于日志分析 + InfluxDB sink 采集）
        _log = logger.bind(
            worker=str(worker_id or ''),
            account=username,
            country=country,
            asin=asin,
            ip=real_ip,
        )
        self._log = _log   # 供 get_reviews_begin / get_more_reviews 等内部方法使用

        if self.account_info:
            is_need_release_account = False
        else:
            is_need_release_account = True

        # 发射任务开始事件
        try:
            push_event(_redis, EventType.TASK_START,
                       username=username, asin=asin, country=country,
                       worker_id=str(worker_id or ''))
        except Exception:
            pass

        attempt = 0

        def _close_current_browser_for_switch() -> None:
            if self.page is None:
                return
            try:
                self._close_browser()
                self._quit_fingerprint_browser()
            except Exception:
                _log.warning(f'关闭指纹浏览器异常: {traceback.format_exc()}')

        def _switch_account_for_retry(reason: str, *, disable_current: bool = False) -> bool:
            """
            当前账号/代理组合失败后切到新账号。
            除 ASIN 重定向外，reviews.py 的异常都走这里，避免同一账号反复重试。
            """
            nonlocal account_manager, username, real_ip, _log

            if not account_manager:
                account_manager = AccountManager(worker_id)

            switch_country = task.get('country') or country
            failed_account = self.account_info
            failed_username = getattr(failed_account, 'username', '') or username

            try:
                account_manager.force_release(country=switch_country)
            except Exception:
                _log.warning(f"[换号] force_release 异常: {traceback.format_exc()}")

            _close_current_browser_for_switch()

            try:
                if failed_account:
                    failed_account.cooldown_until = time.time() + 20 * 60
                    if disable_current:
                        failed_account.state = -1
                    account_manager._save_account(failed_account)
            except Exception:
                _log.warning(f"[换号] 保存账号冷却状态失败: {traceback.format_exc()}")

            account = account_manager.get_account({'country': switch_country})
            if not account or account.username == failed_username:
                _log.error(
                    f"进程{worker_id}-任务{task.get('id', '')}：无其他可用账号，"
                    f"failed={failed_username}, reason={reason[:200]}"
                )
                return False

            self.account_info = account
            username = account.username
            self.cookies = None
            self.reviews_csrf_token = ''
            self._proxy_ip_cache = ''
            try:
                real_ip = self._resolve_proxy_ip()
            except Exception:
                real_ip = ''
            _log = logger.bind(
                worker=str(worker_id or ''),
                account=username,
                country=country,
                asin=asin,
                ip=real_ip,
            )
            self._log = _log
            _log.info(f"[换号] {failed_username} -> {username}, reason={reason[:200]}")
            return True

        while retry_left >= 0:
            try:
                _log.info(
                    f"进程{worker_id}：执行ASIN[{asin}]，账号[{username}]，剩余重试{retry_left}"
                )
                reviews = self.get_reviews_begin(task)
                success = True
                review_count = len(reviews) if reviews else 0
                # 估算页面数：平均每页 10 条评论，向上取整
                pages_fetched = max(1, (review_count + 9) // 10)
                self._record_usage_log(
                    task, _log_start_time, success=True,
                    review_count=review_count,
                    retry_count=RETRY_TIMES - retry_left, worker_id=worker_id,
                )
                # 成功：重置账号异常计数
                try:
                    reset_account_error(username, _redis)
                except Exception:
                    pass
                # InfluxDB：上报请求成功指标
                try:
                    _rpt = get_reporter()
                    if _rpt:
                        _rpt.account.report_status(
                            account_id=username, site=country, status="active",
                        )
                except Exception:
                    pass
                # 发射任务成功事件
                try:
                    push_event(_redis, EventType.TASK_SUCCESS,
                               username=username, asin=asin, country=country,
                               worker_id=str(worker_id or ''),
                               extra={"review_count": review_count,
                                      "pages_fetched": pages_fetched,
                                      "retry_count": RETRY_TIMES - retry_left})
                except Exception:
                    pass
                return reviews
            except Exception as exc:
                last_error = exc
                trace_text = traceback.format_exc()

                if '有新asin' in trace_text:
                    self.reviews_csrf_token = ''
                    _log.info(f"[ASIN重定向] 检测到新 ASIN，继续使用当前账号重试: {str(exc)[:200]}")
                    continue

                retry_left -= 1
                attempt += 1

                _err_str = str(exc)
                is_cookie_refresh_exhausted = (
                    isinstance(exc, CookieRefreshExhaustedException)
                    or 'COOKIE_REFRESH_EXHAUSTED' in trace_text
                )
                is_account_switch_required = (
                    isinstance(exc, AccountSwitchRequiredException)
                    or 'ACCOUNT_SWITCH_REQUIRED' in trace_text
                    or 'AccountSwitchRequiredException' in trace_text
                )

                # 登录失败：账号能登录但本次登录动作失败（密码/Cookie/代理问题），
                # 不代表账号被封，只切换账号重试，不计入封号计数
                # 注：get_reviews_begin 会把所有异常包装成 Exception(f'任务失败...traceback')，
                # 所以无法用 isinstance 判断，改用 traceback 文本检测
                is_login_failure = (
                    '登录失败' in trace_text
                    or 'ProfileNotFoundError' in trace_text
                    or 'AccountLoginError' in trace_text
                    or '指纹浏览器代理持续失效' in trace_text
                    or 'Non-base32 digit found' in trace_text
                )

                is_proxy_or_page_error = any(
                    token in trace_text or token in _err_str
                    for token in (
                        '重新请求主页地址',
                        '首页获取失败',
                        '评论数匹配失败',
                        '访问商品页被重定向到登录/验证码',
                        'proxy_error_count',
                        'ProxyError',
                        'HTTPSConnectionPool',
                        'Connection aborted',
                        'curl: (',
                        'TLS connect error',
                    )
                )
                # 只有明确疑似封号才算封禁；代理/页面重试失败不再按封号处理。
                is_definite_ban = (
                    '可能被封' in trace_text
                    and not is_login_failure
                    and not is_account_switch_required
                    and not is_proxy_or_page_error
                )
                is_captcha = 'captcha' in trace_text.lower() or 'robot' in trace_text.lower()

                # DrissionPage 底层仍是 Chrome，网络错误特征与 Playwright 一致；
                # 统一转换为 NetworkException 便于 isinstance 判断
                if not isinstance(exc, NetworkException) and (
                    'net::ERR_' in _err_str
                    or 'timeout' in _err_str.lower()
                    or 'ProxyError' in _err_str
                    or 'HTTPSConnectionPool' in _err_str
                    or 'Connection aborted' in _err_str
                ):
                    exc = NetworkException(_err_str)
                is_network_error = isinstance(exc, NetworkException)

                if is_cookie_refresh_exhausted:
                    _log.warning(f"[Cookie] Cookie 刷新超过上限，切换账号重试: {username}")

                if is_network_error:
                    # 网络异常：不计入账号风控，但统计全局并发数
                    try:
                        net_err_count = increment_network_error(_redis)
                        _log.warning(f"[网络异常] worker={worker_id} 全局并发网络异常数={net_err_count}: {_err_str[:200]}")
                        if net_err_count >= NETWORK_ERR_ALERT_THRESHOLD:
                            if should_alert_network_error(_redis):
                                mark_network_alert_sent(_redis)
                                send_custom_robot_group_message(
                                    f"[网络异常告警] 5分钟内 {net_err_count} 个进程出现网络错误，"
                                    f"可能需要人工检查网络/代理。最近错误: {_err_str[:300]}",
                                    at_mobiles=['17398238551']
                                )
                    except Exception:
                        pass
                elif is_definite_ban:
                    # 封号/业务异常才计入账号异常计数，驱动 BanAnalyzer 配额自动降低
                    # 登录失败不计入，避免误判为风控封号
                    try:
                        err_count = increment_account_error(username, _redis)
                        _log.warning(f"账号 {username} 累计异常次数={err_count}")
                    except Exception:
                        pass

                # InfluxDB：上报封禁/异常事件
                try:
                    _rpt = get_reporter()
                    if _rpt and is_definite_ban:
                        _rpt.account.report_ban(
                            account_id=username, site=country,
                            reason=BanReason.ACCOUNT_BLOCKED,
                        )
                    elif _rpt and is_cookie_refresh_exhausted:
                        _rpt.account.report_ban(
                            account_id=username, site=country,
                            reason=BanReason.COOKIE_EXPIRED,
                        )
                    elif _rpt and is_login_failure:
                        _rpt.account.report_ban(
                            account_id=username, site=country,
                            reason=BanReason.LOGIN_FAILED,
                        )
                    elif _rpt and is_captcha:
                        _rpt.account.report_ban(
                            account_id=username, site=country,
                            reason=BanReason.CAPTCHA,
                        )
                except Exception:
                    pass

                # 发射风控相关事件
                try:
                    if is_definite_ban:
                        push_event(_redis, EventType.ACCOUNT_BANNED,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''),
                                   error_msg=str(exc)[:500])
                    elif is_captcha:
                        push_event(_redis, EventType.CAPTCHA_HIT,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''))
                    else:
                        if is_login_failure:
                            error_prefix = "[登录失败]"
                        elif is_network_error:
                            error_prefix = "[网络异常]"
                        elif is_account_switch_required or is_proxy_or_page_error or is_cookie_refresh_exhausted:
                            error_prefix = "[换号]"
                        else:
                            error_prefix = "[异常换号]"
                        push_event(_redis, EventType.RETRY,
                                   username=username, asin=asin, country=country,
                                   worker_id=str(worker_id or ''),
                                   error_msg=f"{error_prefix} {str(exc)[:300]}",
                                   extra={"attempt": attempt, "retry_left": retry_left})
                except Exception:
                    pass

                if is_login_failure or is_definite_ban or is_account_switch_required:
                    try:
                        alert_prefix = (
                            "[账号异常已停用]" if is_definite_ban
                            else "[登录失败告警]" if is_login_failure
                            else "[换号告警]"
                        )
                        send_custom_robot_group_message(
                            f"{alert_prefix} account={username}, country={country}, asin={asin}, "
                            f"task_id={task.get('id', '')}, reason={str(exc)[:300]}",
                            at_mobiles=['17398238551']
                        )
                    except Exception:
                        pass

                if retry_left >= 0:
                    reason = (
                        "definite_ban" if is_definite_ban
                        else "login_failure" if is_login_failure
                        else "network_error" if is_network_error
                        else "account_switch_required" if is_account_switch_required
                        else "proxy_or_page_error" if is_proxy_or_page_error
                        else "captcha" if is_captcha
                        else "generic_error"
                    )
                    if not _switch_account_for_retry(str(exc), disable_current=is_definite_ban):
                        retry_left = -1
                        time.sleep(60 * 2)
                        raise Exception(f'无账号可用:{task}')

                _log.error(f"任务执行失败，剩余重试{retry_left}：{trace_text}")

                if retry_left >= 0:
                    if is_network_error:
                        # 网络异常退避：根据并发异常数动态加长等待
                        try:
                            net_count = get_network_error_count(_redis)
                        except Exception:
                            net_count = 1
                        if net_count >= NETWORK_ERR_MULTI_THRESHOLD:
                            # 多进程同时网络异常，说明可能是全局网络问题，等更长时间
                            backoff = min(30.0 * net_count, 300.0)
                            _log.warning(
                                f"[网络异常] 并发异常数={net_count}，延长等待 {backoff:.0f}s"
                            )
                        else:
                            backoff = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                        backoff += random.uniform(0, backoff * 0.2)
                    else:
                        # 指数退避：base^attempt，上限 RETRY_BACKOFF_MAX
                        backoff = min(RETRY_BACKOFF_BASE ** attempt, RETRY_BACKOFF_MAX)
                        backoff += random.uniform(0, backoff * 0.3)  # 加抖动
                    _log.info(f"重试退避等待 {backoff:.1f}s (attempt={attempt}, network={is_network_error}, reason={reason})")
                    time.sleep(backoff)
            finally:
                try:
                    if is_need_release_account and success:
                        # 本次任务已统计了 pages_fetched，传给 release_account
                        pages_fetched = max(1, (review_count + 9) // 10) if success and review_count else 1
                        account_manager = AccountManager(worker_id)
                        account_manager.release_account(
                            self.account_info, task['asin'], success, task.get('id'),
                            pages_fetched=pages_fetched)
                except Exception:
                    ...
                # 关闭指纹浏览器（成功/无评论/ASIN无效/重试耗尽 均在此统一处理）
                if self.page is not None:
                    try:
                        self._close_browser()
                        self._quit_fingerprint_browser()
                    except Exception:
                        _log.warning(f'关闭指纹浏览器异常: {traceback.format_exc()}')

        if is_need_release_account:
            account_manager = AccountManager()
            account_manager.release_account(self.account_info, task['asin'], success, task.get('id'),
                                            pages_fetched=0)

        self._record_usage_log(
            task, _log_start_time, success=False,
            retry_count=RETRY_TIMES, error_msg=last_error,
            worker_id=worker_id,
        )
        # 发射任务失败事件
        try:
            push_event(_redis, EventType.TASK_FAILED,
                       username=username, asin=asin, country=country,
                       worker_id=str(worker_id or ''),
                       error_msg=str(last_error)[:500])
        except Exception:
            pass
        send_custom_robot_group_message(f'任务重试耗尽: {task}-进程{worker_id}-{last_error}', at_mobiles=['17398238551'])
        raise Exception(f"任务重试耗尽: {task}, last_error={last_error}")


if __name__ == '__main__':
    from app.crawlers.amazon_crawler.shuler.services.amazon.account_ import Account

    task = {   'asin': 'B0DQBJ75KV',
        'country': 'US',
        'id': '111111',
               'max_pages': 3,
        'query_conditions':{}
}
    account_manager = AccountManager('1')
#12816641404 密码错  16268351448 验证
    account = account_manager.scheduler._select_account({'username':'12816641404'})
    review = Reviews(None,task)
    # review.use_local_browser = True  # 启用本地Chrome替代指纹浏览器（免费，多账号隔离）
    review.get_reviews_main(task)
