import json
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from loguru import logger

from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_base import AmazonBase
from app.crawlers.amazon_crawler.shuler.services.amazon.amazon_config import PROXY_MAPPING, SITE_MAPPING
from app.crawlers.amazon_crawler.shuler.util.send_robot_msg import send_custom_robot_group_message

_NOT_FOUND = object()  # sentinel: 404，商品不存在

# Unicode RTL/LTR 控制字符（亚马逊 detailBullets 里大量出现，会污染 key/value 分割）
_UNICODE_MARKS = str.maketrans('', '', '‏‎​‌‍﻿')


def _infer_dim_order(dim_map: dict, var_values: dict, fallback: list) -> list:
    """
    从 dimensionToAsinMap 的 key（如 "0_1_2"）推断各维度顺序。
    通过比较每个位置的最大索引+1 与 variationValues 数组长度，匹配出维度到位置的映射。
    当所有维度长度唯一时（常见），结果确定；有歧义时回退 fallback 顺序。
    """
    num_dims = max((len(k.split('_')) for k in dim_map), default=0)
    if num_dims and var_values:
        max_indices = [0] * num_dims
        for k in dim_map:
            for i, p in enumerate(k.split('_')[:num_dims]):
                try:
                    max_indices[i] = max(max_indices[i], int(p))
                except ValueError:
                    pass
        dim_lengths = {d: len(v) for d, v in var_values.items() if isinstance(v, (list, dict))}
        unassigned = list(var_values.keys())
        pos_map: dict[int, str] = {}
        for i, max_idx in enumerate(max_indices):
            target = max_idx + 1
            matches = [d for d in unassigned if dim_lengths.get(d) == target]
            if len(matches) == 1:
                pos_map[i] = matches[0]
                unassigned.remove(matches[0])
        if len(pos_map) == num_dims:
            return [pos_map[i] for i in range(num_dims)]
    return list(fallback)

# Amazon 各站点自营 seller ID（用于 FBA 判断）
AMAZON_SELLER_IDS = {
    'US': 'ATVPDKIKX0DER',
    'UK': 'A3P5ROKL5A1OLE',
    'DE': 'A3JWKAKR8XB7XF',
    'JP': 'AN1VRQENFRJN5',
    'CA': 'A2EUQ1WTGCTBG2',
    'FR': 'A1X6FK5RDHNB96',
    'IT': 'A11IL2PNWYJU7H',
    'ES': 'A1RKKUPIHCS9HS',
    'AU': 'A39IBJ37TRP1C6',
    'IN': 'A14CZOWI0VEHLG',
    'BR': 'A1Q2Y0B3NL8888',
    'MX': 'A1AM78C64UM0Y8',
}

# 第一层：CAPTCHA/Bot 检测（HTML 标签，全站点通用）
BOT_SIGNALS = [
    'captchacharacters',
    '/errors/validatecaptcha',
    'auth-captcha-image',
    'opfcaptcha.amazon.com',
    'continue shopping',
    'robot check',
    'not a robot',
    'enter the characters you see below',
    'automated access',
]

# 第二层：商品页识别（HTML 标签，全站点通用）
PRODUCT_PAGE_SIGNALS = [
    'id="productTitle"',
    'id="title_feature_div"',
    'id="add-to-cart-button"',
    'id="buy-now-button"',
    'id="feature-bullets"',
    'id="acrCustomerReviewText"',
]

PRODUCT_PAGE_PATTERNS = [
    r'id\s*=\s*["\']productTitle["\']',
    r'id\s*=\s*["\']dp-container["\']',
    r'id\s*=\s*["\']ppd["\']',
    r'id\s*=\s*["\']landingImage["\']',
    r'id\s*=\s*["\']imgTagWrapperId["\']',
    r'id\s*=\s*["\']twister["\']',
    r'id\s*=\s*["\']detailBullets(?:Wrapper)?_feature_div["\']',
    r'id\s*=\s*["\']productDetails_feature_div["\']',
    r'name\s*=\s*["\']ASIN["\']',
    r'"dimensionToAsinMap"\s*:',
    r'"parentAsin"\s*:',
]


class ParseError(Exception):
    pass


_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
]


class ASINS(AmazonBase):
    def __init__(self, task, user_agents: list[str] = None):
        # 传 task=None 避免 AmazonBase 触发账号加载（匿名请求不需要账号）
        AmazonBase.__init__(self, account_info=None, task=None)
        self.task = task
        # 不传则从全局池随机取2个，由调用方传入则使用调用方的固定子集
        self._user_agents = user_agents or random.sample(_USER_AGENTS, 2)
        self._base_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }

    def _make_proxy(self) -> dict:
        #curl -x  -U "PsFaJMphAU0hH1s20E-zone-adam-session-153fkxhag:iWrz7GbWhm@2345" ipinfo.io
        country = self.task['country'].upper()
        region = PROXY_MAPPING.get(country, 'us')
        session_id = int(time.time() * 1000)
        proxy = (
            f'http://PsFaJMphAU0hH1s20E-zone-custom-region-{region}'
            f'-session-{session_id}-sessTime-5'
            f':iWrz7GbWhm@a477c1a8e06d7ff8.qzc.na.grassdata.net:2333'
        )
        return {'http': proxy, 'https': proxy}

    # ── 第一层：Bot 检测 ──────────────────────────────────────────────────────
    def _detect_bot(self, html: str) -> bool:
        lower_html = (html or '').lower()
        return any(signal in lower_html for signal in BOT_SIGNALS)

    # ── 第二层：商品页类型识别 ────────────────────────────────────────────────
    def _is_product_page(self, html: str) -> bool:
        if any(signal in html for signal in PRODUCT_PAGE_SIGNALS):
            return True
        return any(re.search(pattern, html, re.I) for pattern in PRODUCT_PAGE_PATTERNS)

    # ── 解析方法 ──────────────────────────────────────────────────────────────

    def _parse_core(self, soup: BeautifulSoup) -> dict:
        result = {}

        # title
        el = soup.find(id='productTitle')
        result['title'] = el.text.strip() if el else None

        # brand & brandUrl
        byline = soup.find(id='bylineInfo')
        if byline:
            # bylineInfo 可能本身就是 <a>，也可能是包含 <a> 的容器
            a_tag = byline if byline.name == 'a' else byline.find('a')
            if a_tag and a_tag.get('href'):
                domain = SITE_MAPPING[self.task['country'].upper()]
                result['brandUrl'] = f'https://{domain}{a_tag["href"]}'
            else:
                result['brandUrl'] = None
            text = byline.text.strip()
            m = re.search(r'Visit the (.+?) Store', text)
            result['brand'] = m.group(1).strip() if m else text
        else:
            result['brand'] = None
            result['brandUrl'] = None

        # price
        whole = soup.find('span', {'class': 'a-price-whole'})
        frac = soup.find('span', {'class': 'a-price-fraction'})
        if whole:
            whole_str = whole.text.strip().replace(',', '').rstrip('.')
            frac_str = frac.text.strip() if frac else '00'
            try:
                result['price'] = float(f'{whole_str}.{frac_str}')
            except ValueError:
                result['price'] = None
        else:
            result['price'] = None

        # rating
        rating_el = soup.find('span', {'class': 'a-icon-alt'})
        if rating_el:
            m = re.search(r'(\d+\.?\d*)', rating_el.text)
            result['rating'] = float(m.group(1)) if m else None
        else:
            result['rating'] = None

        # ratings：星级评分数量
        rc = soup.find('span', {'id': 'acrCustomerReviewText'})
        if rc:
            m = re.search(r'([\d,]+)', rc.text)
            result['ratings'] = int(m.group(1).replace(',', '')) if m else None
        else:
            result['ratings'] = None

        # reviews：文字评论数（与 ratings 独立，可能不同）
        reviews_link = (soup.find('a', {'data-hook': 'see-all-reviews-link-foot'})
                        # or soup.find(id='acrCustomerReviewLink')
                        )
        if reviews_link:
            m = re.search(r'([\d,]+)', reviews_link.get_text())
            result['reviews'] = int(m.group(1).replace(',', '')) if m else None
        else:
            result['reviews'] = None

        # questions（AJAX 异步加载，初始 HTML 可能缺失）
        qa = soup.find(id='askATFLink') or soup.find(id='ask')
        if qa:
            m = re.search(r'([\d,]+)', qa.text)
            result['questions'] = int(m.group(1).replace(',', '')) if m else None
        else:
            result['questions'] = None

        # availability
        av = soup.find(id='availability')
        result['availability'] = av.text.strip() if av else None

        return result

    @staticmethod
    def _extract_bsr_from_td(td, result: dict):
        """从含 BSR 文本的 <td> 或 <span> 中提取排名数据，填充 result。"""
        ranks = re.findall(r'#([\d,]+)', td.get_text())
        cat_links = td.find_all('a')
        sub = []
        for i, cat_link in enumerate(cat_links):
            if i >= len(ranks):
                break
            rank = int(ranks[i].replace(',', ''))
            label = cat_link.text.strip()
            see_top_m = re.match(r'^See Top \d+ in (.+)$', label)
            if see_top_m:
                label = see_top_m.group(1).strip()
            href = cat_link.get('href', '')
            node_m = re.search(r'/bestsellers/[^/]+/(\d+)', href) \
                or re.search(r'/bestsellers/([^/?]+)', href)
            node_id = node_m.group(1) if node_m else None
            if i == 0:
                result['bsrRank'] = rank
                result['bsrLabel'] = label
                result['bsrId'] = node_id
                result['nodeId'] = node_id
            else:
                sub.append({'rank': rank, 'label': label, 'nodeId': node_id})
        if sub:
            result['subcategories'] = sub
        if result['bsrLabel']:
            all_labels = [result['bsrLabel']] + [s['label'] for s in sub]
            result['nodeLabelPath'] = ' > '.join(all_labels)

    def _parse_bsr(self, soup: BeautifulSoup) -> dict:
        result = {
            'bsrRank': None, 'bsrLabel': None, 'bsrId': None,
            'subcategories': None, 'nodeLabelPath': None, 'nodeId': None,
        }

        # ── Layout 1: productDetails expanderTables（主流桌面版） ──────────────
        right = soup.find(id='productDetails_expanderTables_depthRightSections')
        if right:
            for row in right.find_all('tr'):
                td = row.find('td')
                if td and re.search(r'#\s*[\d,]+', td.get_text()):
                    self._extract_bsr_from_td(td, result)
                    break
            if result['bsrRank']:
                return result

        # ── Layout 2: detailBullets（简洁版/部分品类） ────────────────────────
        for bullet_id in ('detailBulletsWrapper_feature_div', 'detailBullets_feature_div'):
            section = soup.find(id=bullet_id)
            if not section:
                continue
            for item in section.find_all('li'):
                text = item.get_text()
                if 'Best Sellers Rank' not in text:
                    continue
                if re.search(r'#\s*[\d,]+', text):
                    self._extract_bsr_from_td(item, result)
                    break
            if result['bsrRank']:
                return result

        # ── Layout 3: <th>Best Sellers Rank</th> + <td>（旧版/部分站点） ──────
        for th in soup.find_all('th'):
            if 'Best Sellers Rank' in th.get_text():
                td = th.find_next_sibling('td')
                if td and re.search(r'#\s*[\d,]+', td.get_text()):
                    self._extract_bsr_from_td(td, result)
                    break

        return result

    def _parse_details(self, soup: BeautifulSoup) -> dict:
        # ── 1. Product Overview（服装/美妆：面料、护理、产地等） ──────────────
        overview_kv = {}
        el = soup.find(id='productOverview_feature_div')
        if el:
            for row in el.find_all('tr'):
                cells = row.find_all('td')
                if len(cells) >= 2:
                    k = cells[0].get_text(strip=True)
                    v = cells[1].get_text(strip=True)
                    if k and v:
                        overview_kv[k] = v

        # ── 2. Product Details 表格（电子/书籍：尺寸、重量、ASIN 等） ────────
        detail_kv = {}
        for table_id in (
            'productDetails_expanderSectionTables',
            'productDetails_expanderTables_depthLeftSections',
            'productDetails_expanderTables_depthRightSections',
        ):
            el = soup.find(id=table_id)
            if not el:
                continue
            for row in el.find_all('tr'):
                th, td = row.find('th'), row.find('td')
                if th and td:
                    k = th.get_text(strip=True)
                    v = re.sub(r'\s+', ' ', td.get_text(' ', strip=True))
                    if k and v:
                        detail_kv.setdefault(k, v)

        # ── 3. Detail bullets（紧凑版；文本含 Unicode RTL/LTR 控制字符需先清除） ──
        for bid in ('detailBulletsWrapper_feature_div', 'detailBullets_feature_div'):
            el = soup.find(id=bid)
            if not el:
                continue
            for li in el.find_all('li'):
                text = li.get_text(' ', strip=True).translate(_UNICODE_MARKS).strip()
                if ':' in text and 'Best Sellers' not in text:
                    k, _, v = text.partition(':')
                    k, v = k.strip(), v.strip()
                    if k and v:
                        detail_kv.setdefault(k, v)

        all_kv = {**detail_kv, **overview_kv}
        dimensions = weight = available_date = None
        for k, v in all_kv.items():
            kl = k.lower()
            if ('dimension' in kl or 'package size' in kl) and not dimensions:
                dimensions = v
            elif 'weight' in kl and not weight:
                weight = v
            elif ('date first' in kl or 'first available' in kl) and not available_date:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(v.strip(), '%B %d, %Y')
                    available_date = int(dt.timestamp() * 1000)
                except Exception:
                    pass

        return {
            'overviews': json.dumps(overview_kv, ensure_ascii=False) if overview_kv else None,
            'dimensions': dimensions,
            'weight': weight,
            'availableDate': available_date,
        }

    def _parse_images(self, html: str) -> dict:
        hires = re.findall(r'"hiRes"\s*:\s*"(https://[^"]+)"', html)
        seen, unique = set(), []
        for img in hires:
            if img not in seen:
                seen.add(img)
                unique.append(img)
        return {
            'imageUrl': unique[0] if unique else None,
            'zoomImageUrl': unique if unique else None,
        }

    def _parse_seller(self, soup: BeautifulSoup, html: str) -> dict:
        result = {}

        sel_el = soup.find('a', {'id': 'sellerProfileTriggerId'})
        result['sellerName'] = sel_el.text.strip() if sel_el else None

        mid_el = soup.find(id='merchantID')
        seller_id = mid_el.get('value') if mid_el else None
        result['sellerId'] = seller_id

        # FBA/FBM 判断（多种页面布局兼容）
        fulfillment = None
        ime_el = soup.find(id='isMerchantExclusive')
        if ime_el:
            fulfillment = 'FBM' if ime_el.get('value') == '1' else 'FBA'
        else:
            for eid in ('merchantInfoFeature_feature_div', 'merchant-info',
                        'tabular-buybox-container', 'desktop_buybox_group_1'):
                el = soup.find(id=eid)
                if not el:
                    continue
                text = el.get_text(' ', strip=True)
                # "Ships from" 优先判断：Amazon 自营发货 → FBA，否则 → FBM
                ships_m = re.search(r'Ships\s+from\s+([^.\n]+?)(?:\s{2,}|$)', text, re.I)
                if ships_m:
                    fulfillment = 'FBA' if re.search(r'Amazon', ships_m.group(1), re.I) else 'FBM'
                    break
                # 无 "Ships from" 则用 "Sold by" / "Seller" 兜底
                if re.search(r'(?:Sold\s+by|Seller)\s+Amazon(?:\.com)?', text, re.I):
                    fulfillment = 'FBA'
                    break
                if re.search(r'(?:Sold\s+by|Seller)\s+\w', text, re.I):
                    fulfillment = 'FBM'
                    break
            if fulfillment is None and seller_id and seller_id in AMAZON_SELLER_IDS.values():
                fulfillment = 'FBA'
        result['fulfillment'] = fulfillment

        # 卖家数量：多种文案兼容
        sellers = None
        for pat in (
            r'(\d+)\s+(?:new|used|new\s+&\s+used\s+)?offers?',           # "2 new offers"
            r'See\s+all\s+(\d+)\s+(?:buying\s+)?options',                 # "See all 3 buying options"
            r'(\d+)\s+(?:used\s+&?\s*new|new\s+&?\s*used)\s+offers?',    # "5 used & new offers"
            r'>(\d+)\s+(?:new|sellers?)',                                  # DOM text node fallback
        ):
            m = re.search(pat, html, re.I)
            if m:
                sellers = int(m.group(1))
                break
        result['sellers'] = sellers

        return result

    def _parse_variations(self, html: str) -> dict:
        result = {
            'parent': None, 'variations': None,
            'variationList': None, 'skuList': None,
        }

        parent_m = re.search(r'"parentAsin"\s*:\s*"([A-Z0-9]{10})"', html)
        result['parent'] = parent_m.group(1) if parent_m else None

        dim_m = re.search(r'"dimensionToAsinMap"\s*:\s*({[^}]+})', html)
        if not dim_m:
            return result
        try:
            cleaned = re.sub(r',\s*}', '}', dim_m.group(1))
            dim_map = json.loads(cleaned)
        except Exception:
            return result

        unique_asins = list(dict.fromkeys(dim_map.values()))
        result['variations'] = len(unique_asins)

        # 尝试提取维度名称和值，构建带属性描述的 variationList
        var_values: dict = {}   # {"color_name": {"0": "Black", "1": "Navy", ...}, ...}
        var_labels: dict = {}   # {"color_name": "Color", "size_name": "Size", ...}

        # variationValues: 值是字符串数组，对象内无嵌套 {}，用专用正则避免 lookahead 限制
        for m in re.finditer(
            r'"variationValues"\s*:\s*(\{(?:[^{}]|\[[^\[\]]*\])*\})',
            html
        ):
            try:
                candidate = json.loads(m.group(1))
                if isinstance(candidate, dict) and candidate:
                    var_values = candidate
                    break
            except Exception:
                continue

        labels_m = re.search(r'"variationDisplayLabels"\s*:\s*({[^}]+})', html)
        if labels_m:
            try:
                var_labels = json.loads(labels_m.group(1))
            except Exception:
                pass

        if var_values and var_labels:
            # 从 dimensionToAsinMap key 推断正确的维度顺序（variationDisplayLabels 的 key 顺序不可靠）
            dim_order = _infer_dim_order(dim_map, var_values, list(var_labels.keys()))
            variation_list = []
            seen: set = set()
            for key_str, asin in dim_map.items():
                if asin in seen:
                    continue
                seen.add(asin)
                parts = key_str.split('_')
                attrs = []
                for i, dim_key in enumerate(dim_order):
                    if i >= len(parts):
                        break
                    idx = parts[i]
                    label = var_labels.get(dim_key, dim_key)
                    values = var_values.get(dim_key, {})
                    if isinstance(values, list):
                        try:
                            val = values[int(idx)]
                        except (IndexError, ValueError):
                            val = idx
                    elif isinstance(values, dict):
                        val = values.get(idx, values.get(int(idx) if idx.isdigit() else idx, idx))
                    else:
                        val = idx
                    attrs.append(f'{label}: {val}')
                variation_list.append({'asin': asin, 'attribute': ' | '.join(attrs)})
            result['variationList'] = variation_list

            # skuList：当前 ASIN 的已选属性列表
            current = self.task.get('asin', '')
            for item in variation_list:
                if item['asin'] == current and item.get('attribute'):
                    result['skuList'] = [a.strip() for a in item['attribute'].split('|')]
                    break
        else:
            # 降级：只返回 ASIN 列表
            result['variationList'] = [{'asin': a} for a in unique_asins]

        return result

    def _parse_badge(self, soup: BeautifulSoup, html: str) -> dict:
        best_seller = bool(
            soup.find(id=re.compile(r'best.?seller', re.I))
            or soup.find(class_=re.compile(r'best.?seller', re.I))
        )
        # 用文本匹配避免 "ac_badge" / acos 等无关元素 ID 被误匹配
        amazons_choice = bool(
            re.search(r"Amazon'?s\s+Choice", html)
            and soup.find(attrs={'id': re.compile(r'amazons.?choice|ac.badge', re.I)})
        ) or bool(
            soup.find(attrs={'class': re.compile(r'ac.badge|amazons.?choice', re.I)})
        )
        new_release = bool(soup.find(id=re.compile(r'new.?release', re.I)))
        ebc = bool(
            soup.find(id=re.compile(r'aplus', re.I))
            or soup.find(class_=re.compile(r'aplus', re.I))
        )
        # 视频：检查 id/class 以及多种 HTML 标志
        video = bool(
            soup.find(id=re.compile(r'dp-video|vse-vdp|video-block', re.I))
            or soup.find(class_=re.compile(r'dp-video|vse-vdp|videoBlock|video-cel', re.I))
            or 'videoCellsContent' in html
            or 'videoBlock' in html
            or '"videoUrl"' in html
            or '"video_url"' in html
        )
        return {
            'bestSeller': 'Y' if best_seller else 'N',
            'amazonChoice': 'Y' if amazons_choice else 'N',
            'newRelease': 'Y' if new_release else 'N',
            'ebc': 'Y' if ebc else 'N',
            'video': 'Y' if video else 'N',
        }

    def _parse_breadcrumb(self, soup: BeautifulSoup) -> dict:
        crumb = soup.find(id='wayfinding-breadcrumbs_feature_div')
        if not crumb:
            return {'nodeLabelPath': None, 'nodeIdPath': None}
        labels, node_ids = [], []
        for a in crumb.find_all('a', href=True):
            text = a.get_text(strip=True)
            m = re.search(r'[?&]node=(\d+)', a['href'])
            if m and text:
                labels.append(text)
                node_ids.append(m.group(1))
        if not labels:
            return {'nodeLabelPath': None, 'nodeIdPath': None}
        return {
            'nodeLabelPath': ':'.join(labels),
            'nodeIdPath': ':'.join(node_ids),
        }

    def _parse_features(self, soup: BeautifulSoup) -> Optional[list]:
        # pqv-feature-bullets 是服装等品类实际使用的 ID；feature-bullets 用于其他品类
        bullets = (soup.find(id='pqv-feature-bullets')
                   or soup.find(id='feature-bullets'))
        if not bullets:
            return None
        items = []
        for li in bullets.find_all('li'):
            span = li.find('span', class_='a-list-item')
            if span:
                text = span.get_text(strip=True)
                if text and text != 'About this item':
                    items.append(text)
        return items or None

    def _parse_coupon(self, soup: BeautifulSoup) -> Optional[str]:
        _ids = ('couponBadgeRegularVpc', 'couponBadge', 'couponText', 'vpcButton',
                'cxpApplyBadgeVpc', 'couponPricingMessage', 'vpcContentWrapper')
        _val_re = re.compile(r'([\d]+%|[\$€£＄][\d.]+)', re.I)
        for eid in _ids:
            el = soup.find(id=eid)
            if el:
                text = el.get_text(strip=True)
                if text:
                    m = _val_re.search(text)
                    return m.group(1) if m else text
        # class-based sweep（Amazon 新版用 class 而非固定 id）
        for el in soup.find_all(class_=re.compile(r'coupon', re.I)):
            text = el.get_text(strip=True)
            m = _val_re.search(text)
            if m:
                return m.group(1)
        # 全文正则兜底，覆盖多种文案格式
        full = soup.get_text()
        for pat in (
            r'Save\s+([\d.]+%|[\$€£＄][\d.]+)\s+(?:with\s+)?coupon',
            r'(?:Apply|Clip)\s+([\d.]+%|[\$€£＄][\d.]+)(?:\s+off)?\s+coupon',
            r'([\d.]+%|[\$€£＄][\d.]+)\s+off\s+coupon',
            r'coupon[:\s]+(?:Save\s+)?([\d.]+%|[\$€£＄][\d.]+)',
        ):
            m = re.search(pat, full, re.I)
            if m:
                return next(g for g in m.groups() if g)
        return None

    @staticmethod
    def _clean_text(value: str | None) -> str:
        return re.sub(r'\s+', ' ', value or '').strip()

    @staticmethod
    def _first_float(value: str | None) -> Optional[float]:
        if not value:
            return None
        m = re.search(r'([\d,]+(?:\.\d+)?)', value)
        if not m:
            return None
        try:
            return float(m.group(1).replace(',', ''))
        except ValueError:
            return None

    @staticmethod
    def _money_values(value: str | None) -> list[float]:
        if not value:
            return []
        out = []
        for raw in re.findall(r'[$€£]\s*([\d,]+(?:\.\d+)?)', value):
            try:
                out.append(float(raw.replace(',', '')))
            except ValueError:
                continue
        return out

    @staticmethod
    def _strip_query(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))

    @staticmethod
    def _dedupe(values: list) -> list:
        seen = set()
        out = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    def _parse_product_information(self, soup: BeautifulSoup) -> dict:
        info = {}
        for table_id in (
            'productOverview_feature_div',
            'productDetails_expanderSectionTables',
            'productDetails_expanderTables_depthLeftSections',
            'productDetails_expanderTables_depthRightSections',
            'productDetails_feature_div',
        ):
            el = soup.find(id=table_id)
            if not el:
                continue
            for row in el.find_all('tr'):
                th = row.find('th')
                cells = row.find_all('td')
                if th and cells:
                    key = self._clean_text(th.get_text(' ', strip=True)).translate(_UNICODE_MARKS)
                    val = self._clean_text(cells[-1].get_text(' ', strip=True)).translate(_UNICODE_MARKS)
                elif len(cells) >= 2:
                    key = self._clean_text(cells[0].get_text(' ', strip=True)).translate(_UNICODE_MARKS)
                    val = self._clean_text(cells[1].get_text(' ', strip=True)).translate(_UNICODE_MARKS)
                else:
                    continue
                if key and val:
                    info.setdefault(key, val)
        return info

    def _parse_price_fields(self, soup: BeautifulSoup, raw: dict) -> dict:
        price_el = soup.find(id='corePriceDisplay_desktop_feature_div') or soup.find(id='corePrice_feature_div')
        price_text = self._clean_text(price_el.get_text(' ', strip=True) if price_el else '')
        amounts = self._money_values(price_text)
        page_price = raw.get('price') or (amounts[0] if amounts else None)
        list_price = None
        typical = re.search(r'Typical price:\s*[$€£]\s*([\d,]+(?:\.\d+)?)', price_text, re.I)
        if typical:
            list_price = self._first_float(typical.group(1))
        elif len(amounts) > 1:
            list_price = max(amounts)
        selling_price_raw = None
        offscreen = soup.select_one('.a-price .a-offscreen')
        if offscreen:
            selling_price_raw = self._clean_text(offscreen.get_text(' ', strip=True))
        elif page_price is not None:
            selling_price_raw = f'${page_price:.2f}'
        return {
            'raw_price': None,
            'regular_price': list_price,
            'selling_price': list_price or page_price,
            'selling_price_raw': selling_price_raw,
            'was_price': list_price,
            'list_price': list_price,
            'with_deal_price': page_price,
            'page_price': page_price,
            'dotd_price': None,
            'prime_member_price': page_price,
            'prime_exclusive_discount': round((list_price or 0) - page_price, 2) if list_price and page_price else None,
            'is_prime_exclusive': 'prime exclusive' in price_text.lower(),
        }

    def _parse_sales_social_proof(self, soup: BeautifulSoup) -> dict:
        el = soup.find(id='socialProofingAsinFaceout_feature_div')
        raw = self._clean_text(el.get_text(' ', strip=True) if el else '')
        value = None
        m = re.search(r'([\d,.]+)\s*([KkMm]?)\+', raw)
        if m:
            num = float(m.group(1).replace(',', ''))
            suffix = m.group(2).lower()
            if suffix == 'k':
                num *= 1000
            elif suffix == 'm':
                num *= 1_000_000
            value = int(num)
        return {'last_month_sales_raw': raw or None, 'last_month_sales': value}

    def _parse_review_star(self, soup: BeautifulSoup) -> dict:
        shares = {}
        for link in soup.find_all(attrs={'aria-label': True}):
            label = link.get('aria-label') or ''
            m = re.search(r'(\d+)\s+percent\s+of\s+reviews\s+have\s+([1-5])\s+stars?', label, re.I)
            if m:
                shares[m.group(2)] = int(m.group(1)) / 100
        return {str(i): shares.get(str(i), 0) for i in range(1, 6)}

    def _parse_content_block(self, soup: BeautifulSoup, element_id: str) -> Optional[dict]:
        el = soup.find(id=element_id)
        if not el:
            return None
        images = []
        for img in el.find_all('img'):
            src = img.get('data-src') or img.get('src')
            if src and 'grey-pixel' not in src:
                images.append(src)
        texts = []
        for text in el.stripped_strings:
            clean = self._clean_text(text)
            if clean:
                texts.append(clean)
        return {'images': self._dedupe(images), 'texts': self._dedupe(texts)}

    def _parse_product_documents(self, soup: BeautifulSoup) -> list[str]:
        urls = []
        el = soup.find(id='productDocuments_feature_div') or soup
        for href in re.findall(r'https://[^"\']+?\.pdf(?:\?[^"\']*)?', str(el)):
            urls.append(self._strip_query(href))
        return self._dedupe(urls)

    def _parse_reviews_from_listing(self, soup: BeautifulSoup) -> list[dict]:
        reviews = []
        seen = set()
        for container in soup.select('[data-hook="reviewContainer"]'):
            review_id = container.get('data-reviewid')
            if not review_id or review_id in seen:
                continue
            seen.add(review_id)
            review = container.select_one('[data-hook="review"]') or container

            def text(selector: str) -> str:
                el = review.select_one(selector)
                return self._clean_text(el.get_text(' ', strip=True) if el else '')

            rating_raw = text('[data-hook="review-star-rating"] .a-icon-alt')
            date_raw = text('[data-hook="review-date"]')
            helpful_raw = text('[data-hook="helpful-vote-statement"]')
            image_urls = []
            for img in review.select('[data-hook="review-image-tile"]'):
                src = img.get('data-src') or img.get('src')
                if src and 'grey-pixel' not in src:
                    image_urls.append(src)
            helpful_count = 0
            helpful_match = re.search(r'([\d,]+)', helpful_raw)
            if helpful_match:
                helpful_count = int(helpful_match.group(1).replace(',', ''))
            reviews.append({
                'review_id': review_id,
                'asin': container.get('data-asin'),
                'author': text('.a-profile-name') or None,
                'rating': self._first_float(rating_raw),
                'rating_raw': rating_raw or None,
                'title': text('[data-hook="reviewTitle"]') or None,
                'date_raw': date_raw or None,
                'country': self._review_country(date_raw),
                'review_date': self._review_date(date_raw),
                'variation': text('[data-hook="format-strip"]') or None,
                'verified_purchase': bool(review.select_one('[data-hook="avp-badge"]')),
                'content': text('[data-hook="reviewRichContentContainer"]') or None,
                'helpful_count': helpful_count,
                'helpful_raw': helpful_raw or None,
                'images': self._dedupe(image_urls),
                'locale': container.get('data-locale'),
                'source_language': container.get('data-sourcelanguage'),
            })
        return reviews

    @staticmethod
    def _review_country(value: str | None) -> Optional[str]:
        m = re.search(r'Reviewed in (?:the )?(.+?) on ', value or '', re.I)
        return m.group(1).strip() if m else None

    @staticmethod
    def _review_date(value: str | None) -> Optional[str]:
        m = re.search(r' on ([A-Za-z]+ \d{1,2}, \d{4})', value or '')
        if not m:
            return None
        try:
            from datetime import datetime
            return datetime.strptime(m.group(1), '%B %d, %Y').date().isoformat()
        except Exception:
            return None

    def _listing_to_shulex(self, raw: dict, soup: BeautifulSoup, html: str) -> dict:
        product_info = self._parse_product_information(soup)
        overview_info = {}
        if raw.get('overviews'):
            try:
                overview_info = json.loads(raw['overviews'])
            except Exception:
                overview_info = {}
        product_info = {**overview_info, **product_info}

        brand = raw.get('brand')
        pqv = soup.find(id='pqv-byline')
        if not brand and pqv:
            brand = re.sub(r'^From\s+', '', self._clean_text(pqv.get_text(' ', strip=True)), flags=re.I)
        brand = brand or product_info.get('Brand')

        ranks = []
        if raw.get('bsrRank'):
            ranks.append({
                'is_main_category': True,
                'sales_rank': raw.get('bsrRank'),
                'links': [{
                    'link_text': f"#{raw.get('bsrRank')} in {raw.get('bsrLabel')}".strip(),
                    'main_category': raw.get('bsrId'),
                    'sub_category': None,
                }],
            })
        for sub in raw.get('subcategories') or []:
            ranks.append({
                'is_main_category': False,
                'sales_rank': sub.get('rank'),
                'links': [{
                    'link_text': f"#{sub.get('rank')} in {sub.get('label')}".strip(),
                    'main_category': raw.get('bsrId'),
                    'sub_category': sub.get('nodeId'),
                }],
            })
        rank_text = ';'.join(
            link.get('link_text', '')
            for row in ranks
            for link in row.get('links', [])
            if link.get('link_text')
        )
        variation_values = {
            str(i): item.get('asin')
            for i, item in enumerate(raw.get('variationList') or [])
            if item.get('asin')
        }
        feature_items = raw.get('features') or []
        feature = '  \n '.join(feature_items) if isinstance(feature_items, list) else feature_items
        price_fields = self._parse_price_fields(soup, raw)
        sales_fields = self._parse_sales_social_proof(soup)
        product_description = self._parse_content_block(soup, 'aplus_feature_div') or {'images': [], 'texts': []}
        aplus_brand_story = self._parse_content_block(soup, 'aplusBrandStory_feature_div') or {'images': [], 'texts': []}
        review_star = self._parse_review_star(soup)
        item_weight = self._first_float(raw.get('weight'))
        availability = raw.get('availability') or ''

        market = str(raw.get('marketplace') or self.task.get('country') or 'US').upper()
        locale = {
            'US': 'en-us',
            'UK': 'en-gb',
            'CA': 'en-ca',
            'AU': 'en-au',
            'DE': 'de-de',
            'FR': 'fr-fr',
            'IT': 'it-it',
            'ES': 'es-es',
            'JP': 'ja-jp',
        }.get(market, market.lower())

        result = {
            'crawl_date': None,
            'http_code': 200,
            'title': raw.get('title'),
            'brand': brand,
            'page_locale': locale,
            'has_bundle': False,
            'proportion_coupon': raw.get('coupon'),
            'direct_discount_coupon': None,
            'promotion': None,
            'direct_promotion': None,
            **price_fields,
            'is_prime_day': False,
            'exist_price': price_fields.get('page_price') is not None,
            'exist_ranks': bool(ranks),
            'sale_statuses': availability,
            'is_used_price': False,
            'model_name': product_info.get('Model Name'),
            'is_frequently_returned': 'frequently returned' in html.lower(),
            'is_customer_usually_keep': 'customers usually keep' in html.lower(),
            'image_url_list': raw.get('zoomImageUrl') or [],
            'is_big_image': bool(raw.get('zoomImageUrl')),
            'main_image_url': raw.get('imageUrl'),
            'review_num': raw.get('ratings') or raw.get('reviews'),
            'review_num_raw': None,
            'star_rate': raw.get('rating'),
            'star_rate_raw': f"{raw.get('rating')} out of 5 stars" if raw.get('rating') is not None else None,
            'bbx_num': raw.get('sellers'),
            'bbx_num_raw': '',
            'bbx_sellerid': raw.get('sellerId'),
            'ranks': ranks,
            'seller_name': raw.get('sellerName'),
            'helpful_reviews': [],
            'cellphones_rank': rank_text,
            'cellphones_rank_format': rank_text,
            'ratings_share': [{'rating_num': str(raw.get('ratings') or ''), 'star_share': {}}],
            'also_boughts': [],
            'also_bought_asins': [],
            'real_asin': raw.get('asin'),
            'pd': None,
            'feature': feature,
            'product_dimensions': self._dimensions_map(raw.get('dimensions')),
            'item_dimensions': product_info.get('Item Dimensions') or raw.get('dimensions'),
            'item_weight': item_weight,
            'shipping_weight': self._first_float(product_info.get('Shipping Weight')),
            'sold_by': raw.get('sellerName'),
            'sold_bys': [raw.get('sellerName')] if raw.get('sellerName') else [],
            'choice_message': None,
            'asin_variation_values': variation_values,
            'stock_on_hand': None,
            'sp_asin': [],
            'sb_brand': [],
            'exist_sale': False,
            'ld_sold_percent': False,
            'product_information': product_info,
            'item_model_number': product_info.get('Model Number') or product_info.get('Item model number'),
            **sales_fields,
            'promo_code': None,
            'code_coupon': raw.get('coupon'),
            'direct_code_coupon': None,
            'promo_code_desc_raw': '',
            'compare_similar_asins': [],
            'ships_from': 'Amazon' if raw.get('fulfillment') == 'FBA' else None,
            'add_to_cart': 'in stock' in availability.lower(),
            'product_document': self._parse_product_documents(soup),
            'product_description': product_description,
            'aplus_brand_story': aplus_brand_story,
            'product_comparison': None,
            'review_star': review_star,
            'is_unavailable': 'unavailable' in availability.lower() or 'currently unavailable' in availability.lower(),
            'bsr_tag': None,
            'nr_tag': None,
            'cpf_tag': None,
            'reviews': self._parse_reviews_from_listing(soup),
        }
        return result

    @staticmethod
    def _dimensions_map(value: str | None) -> Optional[dict]:
        if not value:
            return None
        nums = re.findall(r'([\d.]+)', value)
        if len(nums) < 3:
            return None
        return {'length': nums[0], 'width': nums[1], 'height': nums[2]}

    # ── 第三层：必有字段校验 ──────────────────────────────────────────────────
    def _validate(self, result: dict):
        for field in ('title', 'imageUrl'):
            if not result.get(field):
                raise ParseError(f'Required field missing: {field}, asin={result.get("asin")}')
        for field in ('price', 'rating', 'bsrRank'):
            if result.get(field) is None:
                logger.warning(f'[ASIN:{result.get("asin")}] Optional field missing: {field}')

    # ── 验证码处理 ────────────────────────────────────────────────────────────
    def _handle_captcha_with_browser(self, proxy_url: str, url: str, headers: dict) -> str:
        """静态IP出验证时，启动 Playwright headful 自动过验证，返回通行 cookie 字符串"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[ASIN] playwright 未安装，跳过浏览器验证")
            return ""

        host = proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url
        logger.warning(f"[ASIN] 打开浏览器处理验证码 {host}...")
        try:
            with sync_playwright() as p:
                from urllib.parse import urlparse as _urlparse
                _p = _urlparse(proxy_url)
                proxy_cfg = {"server": f"{_p.scheme}://{_p.hostname}:{_p.port}"}
                if _p.username:
                    proxy_cfg["username"] = _p.username
                if _p.password:
                    proxy_cfg["password"] = _p.password
                browser = p.chromium.launch(
                    headless=True,
                    proxy=proxy_cfg,
                )
                context = browser.new_context(
                    user_agent=headers.get("User-Agent", ""),
                    extra_http_headers={
                        "Accept": headers.get("Accept", ""),
                        "Accept-Language": headers.get("Accept-Language", ""),
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                page = context.new_page()
                page.goto(url, timeout=30_000)
                page.wait_for_selector(
                    'button[alt="Continue shopping"], #productTitle, #title',
                    timeout=60_000,
                )
                btn = page.query_selector('button[alt="Continue shopping"]')
                if btn:
                    btn.click()
                    logger.info("[ASIN] 点击 Continue shopping，等待商品页...")
                    page.wait_for_selector('#productTitle, #title', timeout=30_000)

                # 提取浏览器 cookie，供后续 curl_cffi 携带
                cookies = context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                logger.info(f"[ASIN] 验证通过，提取 {len(cookies)} 个 cookie")
                browser.close()
                return cookie_str
        except Exception as e:
            logger.warning(f"[ASIN] 浏览器验证异常: {e}")
            return ""

    # ── 内部：单代理3次尝试 ───────────────────────────────────────────────────
    def _fetch_html(self, proxy: dict, url: str, initial_cookies: str):
        """
        用给定代理最多请求3次，返回 (html, cookies, try_fallback)：
          - html = _NOT_FOUND  → 404
          - html = None, try_fallback = False → 非商品页（不需要换代理重试）
          - html = None, try_fallback = True  → 3次全失败（可换代理重试）
          - html = str         → 成功
        """
        asin = self.task['asin']
        captcha_cookies = initial_cookies
        self._captcha_cookies = captcha_cookies

        proxy_url = (proxy or {}).get('http', '')
        ip_hint = urlparse(proxy_url).hostname or proxy_url[:30] or 'dynamic'

        for attempt in range(1, 4):
            time.sleep(1)
            proxies = proxy if proxy is not None else self._make_proxy()
            headers = {**self._base_headers, "User-Agent": random.choice(self._user_agents)}
            if captcha_cookies:
                headers["Cookie"] = captcha_cookies
            try:
                resp = self.curl_session.get(
                    url, headers=headers, impersonate='chrome120',
                    proxies=proxies, timeout=25, verify=False,
                )
            except Exception as e:
                logger.warning(f'[ASIN:{asin}] [{ip_hint}] 第{attempt}次请求失败: {e}')
                continue

            if resp.status_code == 404:
                logger.info(f'[ASIN:{asin}] [{ip_hint}] 404 商品不存在')
                self._captcha_cookies = captcha_cookies
                return _NOT_FOUND, captcha_cookies, False

            html = resp.text

            if self._detect_bot(html):
                logger.warning(f'[ASIN:{asin}] [{ip_hint}] 第{attempt}次Bot/CAPTCHA')
                inner_url = proxies.get('http', '') if proxies else ''
                if proxy is not None and 'zone-custom-region-' not in inner_url:
                    captcha_cookies = self._handle_captcha_with_browser(inner_url, url, headers)
                    self._captcha_cookies = captcha_cookies
                continue

            if not self._is_product_page(html):
                final_url = getattr(resp, 'url', '') or ''
                logger.info(
                    f'[ASIN:{asin}] [{ip_hint}] 第{attempt}次非商品页 '
                    f'status={resp.status_code} url={str(final_url)[:160]}'
                )
                self._captcha_cookies = captcha_cookies
                continue

            # 合并 response Set-Cookie（Amazon 定期刷新 session token，必须跟进）
            if resp.cookies:
                existing = {}
                if captcha_cookies:
                    for part in captcha_cookies.split(';'):
                        part = part.strip()
                        if '=' in part:
                            k, v = part.split('=', 1)
                            existing[k.strip()] = v.strip()
                for name, value in resp.cookies.items():
                    existing[name] = value
                captcha_cookies = '; '.join(f'{k}={v}' for k, v in existing.items())
                self._captcha_cookies = captcha_cookies
                logger.debug(f'[ASIN:{asin}] [{ip_hint}] 更新 cookies ({len(existing)} 个)')

            return html, captcha_cookies, False

        logger.error(f'[ASIN:{asin}] [{ip_hint}] 3次全部失败（网络/bot/非商品页）')
        self._captcha_cookies = captcha_cookies
        return None, captcha_cookies, True

    # ── 主入口 ────────────────────────────────────────────────────────────────
    def get_product_detail(self, proxy: dict = None, initial_cookies: str = "") -> Optional[dict]:
        """
        :param proxy:           静态IP代理 dict；None 则每次用动态旋转代理
        :param initial_cookies: 该IP缓存的验证cookie
        静态IP 3次全失败后自动降级动态代理重试。
        """
        asin = self.task['asin']
        country = self.task['country'].upper()
        domain = SITE_MAPPING[country]
        url = f'https://{domain}/dp/{asin}?th=1&language=en_US&psc=1'

        html, cookies, try_fallback = self._fetch_html(proxy, url, initial_cookies)
        self.last_snapshot_html = ""

        if html is _NOT_FOUND:
            return {'asin': asin, 'not_found': True}

        # 静态IP 3次全失败 → 告警 + 动态代理重试
        if html is None and try_fallback and proxy is not None:
            ip_hint = urlparse(proxy.get('http', '')).hostname or 'static'
            logger.warning(f'[ASIN:{asin}] 静态IP={ip_hint} 3次全部失败，告警！降级动态代理重试')
            html, cookies, _ = self._fetch_html(None, url, "")
            if html is _NOT_FOUND:
                return {'asin': asin, 'not_found': True}

        if html is None:
            return None

        self.last_snapshot_html = html
        soup = BeautifulSoup(html, 'lxml')

        result = {
            'asin': asin,
            'asinUrl': f'https://{domain}/dp/{asin}',
            'marketplace': country,
        }
        result.update(self._parse_core(soup))
        result.update(self._parse_bsr(soup))
        # 面包屑提供完整路径，优先覆盖 BSR 的部分路径；失败时保留 BSR 值
        crumb = self._parse_breadcrumb(soup)
        result['nodeIdPath'] = crumb.get('nodeIdPath')
        if crumb.get('nodeLabelPath'):
            result['nodeLabelPath'] = crumb['nodeLabelPath']
        result.update(self._parse_details(soup))
        result.update(self._parse_images(html))
        result.update(self._parse_seller(soup, html))
        result.update(self._parse_variations(html))
        result['features'] = self._parse_features(soup)
        result['badge'] = self._parse_badge(soup, html)
        result['coupon'] = self._parse_coupon(soup)

        # nodeId 取最末级子分类的数字 ID（BSR 的 nodeId 可能是 slug 如 'fashion'）
        if result.get('subcategories'):
            leaf_id = result['subcategories'][0].get('nodeId')
            if leaf_id:
                try:
                    result['nodeId'] = int(leaf_id)
                except (ValueError, TypeError):
                    result['nodeId'] = leaf_id

        try:
            self._validate(result)
        except ParseError as e:
            logger.error(f'[ASIN:{asin}] ParseError: {e}')
            return None

        return self._listing_to_shulex(result, soup, html)

    # ── 原有方法保留 ──────────────────────────────────────────────────────────
    def parse_sub_asin(self, response):
        sub_asins = []
        try:
            if 'dimensionToAsinMap' in response:
                pattern = r'"dimensionToAsinMap"\s*:\s*({[^}]+})'
                match = re.search(pattern, response, re.DOTALL)
                map_json_str = re.sub(r',\s*}', '}', match.group(1).strip())
                dimension_map = json.loads(map_json_str)
                sub_asins = list(set(dimension_map.values()))
            matches = re.findall(r'"parentAsin"\s*:\s*"(.*?)",', response)
            parent_asin = matches[0]
            return parent_asin, sub_asins
        except Exception as e:
            print(f'parse_sub_asin 失败：{e}')
            raise

    def get_sub_asin(self):
        url = f'https://{SITE_MAPPING[self.task["country"].upper()]}/dp/{self.task["asin"]}'
        proxies = self._make_proxy()
        response = self.curl_session.get(
            url,
            headers={**self._base_headers, "User-Agent": random.choice(self._user_agents)},
            impersonate='chrome120',
            proxies=proxies, timeout=20, verify=False,
        )
        return self.parse_sub_asin(response.text)


if __name__ == '__main__':
    import json as _json
    asin_obj = ASINS({'country': 'DE', 'asin': 'B017XYB27K'})
    result = asin_obj.get_product_detail({'http':'http://PsFaJMphAU0hH1s20E:iWrz7GbWhm@38.213.248.183:2333'})
    print(_json.dumps(result, indent=2, ensure_ascii=False))
