# 遨森标杆站点取数与计数逻辑说明

> 本文用于说明各标杆站点的商品数据从哪里取、商品数量如何计数、字段完整性如何校验。  
> 统计范围：遨森原始需求中的 46 个标杆独立站。

## 一、统一计数口径

后台商品数量不是简单按页面展示数字抄录，而是按系统实际识别并入库的商品记录统计。

每个站点会根据网站结构选择不同取数入口：

- 平台商品接口
- sitemap 商品 URL
- 分类页商品列表
- 商品详情页
- 商品详情页中的结构化数据

只要系统确认某条记录是有效商品，并成功入库，就计入“已抓商品数”。

字段完整性另行校验。也就是说：

| 指标 | 说明 |
|---|---|
| 已抓商品数 | 已识别并进入商品库的商品记录数量 |
| 价格完整率 | 已抓商品中，价格字段成功解析的比例 |
| 标题完整率 | 已抓商品中，标题字段可用的比例 |
| 促销完整率 | 已抓商品中，促销或折扣信息可识别的比例 |

所以“已抓商品数”和“价格是否完整”是两个不同指标。

## 二、各站点取数逻辑

| 站点 | 品牌 | 国家 | 主要取数入口 | 商品计数逻辑 | 主要字段来源 |
|---|---|---|---|---|---|
| `songmics_us` | SONGMICS | US | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `songmics_uk` | SONGMICS | UK | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `songmics_de` | SONGMICS | DE | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `songmics_it` | SONGMICS | IT | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `songmics_es` | SONGMICS | ES | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `songmics_fr` | SONGMICS | FR | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `costway_us` | Costway | US | Costway 商品接口 `/api/category`、`/api/products` | 按分类接口返回的商品 SKU 去重计数 | 商品接口提供标题、图片、价格、库存、评分、类目、促销标签 |
| `costway_ca` | Costway | CA | Costway 商品接口 `/api/category`、`/api/products` | 按分类接口返回的商品 SKU 去重计数 | 商品接口提供标题、图片、价格、库存、评分、类目、促销标签 |
| `costway_uk` | Costway | UK | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_de` | Costway | DE | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_it` | Costway | IT | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_es` | Costway | ES | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_fr` | Costway | FR | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_nl` | Costway | NL | sitemap 商品 URL + 商品详情页补采 | 先按 sitemap 识别商品 URL 入库，后续校验详情页字段 | sitemap 提供 URL、标题、图片；价格等字段通过详情页补采校验 |
| `costway_pl` | Costway | PL | 类别页发现商品 + 商品详情页 JSON-LD | 从类别页发现商品 URL，成功解析商品详情页后计数 | 商品详情页结构化数据提供标题、图片、价格、币种、库存状态 |
| `homary_us` | Homary | US | Homary 商品 sitemap + SSR 商品详情页 | 按 sitemap 中 `/item/` 商品 URL 作为目标，详情页解析成功后入库 | 商品详情页 HTML/meta/DOM 提供标题、图片、价格、类目、热销标签 |
| `homary_uk` | Homary | UK | Homary 商品 sitemap + SSR 商品详情页 | 按 sitemap 中 `/item/` 商品 URL 作为目标，详情页解析成功后入库 | 商品详情页 HTML/meta/DOM 提供标题、图片、价格、类目、热销标签 |
| `homary_de` | Homary | DE | Homary 商品 sitemap + SSR 商品详情页 | 按 sitemap 中 `/item/` 商品 URL 作为目标，详情页解析成功后入库 | 商品详情页 HTML/meta/DOM 提供标题、图片、价格、类目、热销标签 |
| `homary_es` | Homary | ES | Homary 商品 sitemap + SSR 商品详情页 | 按 sitemap 中 `/item/` 商品 URL 作为目标，详情页解析成功后入库 | 商品详情页 HTML/meta/DOM 提供标题、图片、价格、类目、热销标签 |
| `homary_fr` | Homary | FR | Homary 商品 sitemap + SSR 商品详情页 | 按 sitemap 中 `/item/` 商品 URL 作为目标，详情页解析成功后入库 | 商品详情页 HTML/meta/DOM 提供标题、图片、价格、类目、热销标签 |
| `yaheetech_us` | Yaheetech | US | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `yaheetech_uk` | Yaheetech | UK | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `vidaxl_us` | Vidaxl | US | 当前无数据 | 当前无商品数据，不计入有效商品覆盖 | 无 |
| `vidaxl_ca` | Vidaxl | CA | 当前无数据 | 当前无商品数据，不计入有效商品覆盖 | 无 |
| `vidaxl_uk` | Vidaxl | UK | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_ie` | Vidaxl | IE | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_de` | Vidaxl | DE | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_it` | Vidaxl | IT | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_es` | Vidaxl | ES | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_fr` | Vidaxl | FR | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_ro` | Vidaxl | RO | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_pt` | Vidaxl | PT | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_nl` | Vidaxl | NL | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `vidaxl_pl` | Vidaxl | PL | 分类页 / 商品 URL / 商品详情页结构化数据 | 按分类页或商品 URL 发现商品，详情页解析成功后计 SKU | 商品详情页 JSON-LD / 页面数据提供标题、价格、库存、EAN、图片 |
| `flexispot_us` | Flexispot | US | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_uk` | Flexispot | UK | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_ca` | Flexispot | CA | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_de` | Flexispot | DE | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_it` | Flexispot | IT | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_es` | Flexispot | ES | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_fr` | Flexispot | FR | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_nl` | Flexispot | NL | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `flexispot_pl` | Flexispot | PL | sitemap 商品 slug + 站内商品详情接口 | sitemap 发现商品 slug，详情接口成功返回 SKU 后计数 | 详情接口提供 SKU、标题、价格、图片、类目、库存状态 |
| `bcp_us` | BEST CHOICE PRODUCTS | US | Shopify 商品接口 `/products.json` | 按 Shopify 商品变体展开后计 SKU；同一 SKU 去重 | 商品接口提供标题、图片、价格、库存、变体、标签 |
| `vonhaus_uk` | VonHaus | UK | sitemap 页面扫描 + 商品详情页 OpenGraph 数据 | sitemap 中页面逐个判别，确认是商品页后计数 | 商品页 meta 提供标题、价格、币种、图片、库存状态 |
| `woltu_de` | Woltu | DE | sitemap 商品 URL + 商品详情页结构化数据 | sitemap 发现候选商品 URL，详情页解析成功后计数 | 商品详情页 JSON-LD / OpenGraph 提供标题、价格、图片、币种、类目 |
