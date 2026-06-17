# 标杆平台验收状态对账

- 生成时间: 2026-06-17 12:02
- 验收表: `/Users/wangxiaokang/Desktop/标杆平台验收报告.xlsx`
- 问题行总数: 111

## 状态汇总

| 状态 | 数量 | 含义 |
| --- | ---: | --- |
| 已代码覆盖 | 102 | 本地代码已有对应页面/API/测试入口，仍建议浏览器或接口抽验。 |
| 待外部数据导入 | 1 | 需要 SimilarWeb/GA/BI/人工文件等第三方指标，重跑抓取本身无法生成。 |
| 待浏览器验收 | 2 | 需要用真实页面做视觉/交互验收。 |
| 待真实数据重跑验证 | 6 | 能力已具备，但必须跑生产抓取任务并看结果才能关闭。 |

## 仍未关闭

| Sheet:行 | 模块 | 问题 | 当前状态 | 下一步 |
| --- | --- | --- | --- | --- |
| 功能点检查:14 | 管理界面 | 标杆网站管理面板-30-Day Sales：标杆网站的近30天销量 | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 功能点检查:15 | 管理界面 | 标杆网站管理面板-30-Day Revenue：标杆网站的近30天销售额，显示当地币种 | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 问题:2 | 验收问题 | 商品库中的商品名称没有抓取完整，货币符号不对 | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 问题:3 | 验收问题 | 深色模式下文字对比度偏低，长时间观看易导致视觉疲劳 | 待浏览器验收 | 相关样式已收敛到页面组件，但还需要用真实页面截图做视觉验收。 |
| 问题:4 | 验收问题 | 标杆网站分析报表数据不全：绝大部分网站只展示了SKU数量，少数网站展示了新增产品数量，极少网站展示30天销量与30天收入，流量与转化率未展示 | 待外部数据导入 | 流量/转化率不是页面抓取稳定产物，后台已提供第三方指标导入/校验入口。 |
| 问题:11 | 验收问题 | 站点报表中采集的SKU数据与标杆网站分析报表中的SKU数据不一致 | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 问题:13 | 验收问题 | 爬取过来的竞品数据没有价格 | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 问题:14 | 验收问题 | 人工统计了34个网站的商品数量，其中有16个网站的数据偏差大于50% | 待真实数据重跑验证 | 后台数据质量页已暴露缺价格、缺促销、SKU 偏差、任务失败和重跑前置条件；关闭需要生产抓取结果。 |
| 问题:15 | 验收问题 | 根据规格书统计了有120条功能，实际完成27条 | 待浏览器验收 | 这是规格覆盖率的总述，需要用本对账表和真实页面逐项复核后关闭。 |

## 已找到代码覆盖的原验收行

| Sheet:行 | 模块 | 问题 | 证据 |
| --- | --- | --- | --- |
| 功能点检查:3 | 管理界面 | 【+Add Tracking】 按钮，进入新增标杆URL数据页面（弹窗） | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:5 | 管理界面 | 该面板仅技术人员可拥有编辑及配置权限（权限） | 前台 /report 和 /app 路由要求登录，报表编辑/导出/自定义动作由 canEdit 按角色门控。 |
| 功能点检查:6 | 管理界面 | 搜索框，用于输入URL/Brand name进筛选 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:8 | 管理界面 | 筛选区： 【Market】下拉选择框，对追踪列表进行站点筛选 【Brand】下拉选择框，对追踪列表进行品牌筛选 【Status】下拉选择框，对追踪列表进行追踪状态筛选 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:9 | 管理界面 | 标杆网站管理面板-Market:国旗展示 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:11 | 管理界面 | 标杆网站管理面板-URL：标杆网站URL，上限150个字符，仅维护网站网址固定部分 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:16 | 管理界面 | 标杆网站管理面板-Updated Time：抓取时间点，格式：{YY/MM/DD 00:00}2023-11-14 15:00 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:17 | 管理界面 | 标杆网站管理面板-Created Time：创建时间（）格式：{YY/MM/DD 00:00}2023-11-14 15:00 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:18 | 管理界面 | 标杆网站管理面板-Creator：创建人 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:20 | 管理界面 | Action：Stop Tracking（暂停抓取） | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:21 | 管理界面 | Action：Edit（编辑）：对Brand品牌、Review Rate 留评率等字段进行纠正，点击编辑后，进入编辑面板 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:22 | 管理界面 | Action：Delete（删除）：在列表中删除标杆网站，需要二次确认 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:23 | 管理界面 | 分页栏，默认10行/页，可以切换10/20/50/100/200 行分页 | TrackingPage + tracking API 已实现新增、搜索筛选、状态、销售收入、时间、创建人、操作和分页。 |
| 功能点检查:26 | Store Analysis 面板 | 该面板仅有权限的人员可查看/编辑（权限） | 前台 /report 和 /app 路由要求登录，报表编辑/导出/自定义动作由 canEdit 按角色门控。 |
| 功能点检查:27 | Store Analysis 面板 | 汇总数据：近30天汇总数据 SKU：SKU数 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:28 | Store Analysis 面板 | 汇总数据：近30天汇总数据 New Products：上行：上新产品数，下行：对比上周期数据 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:33 | Store Analysis 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周/天趋势数据查看 SKU | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:41 | Product Analysis 面板 | 筛选区2：对列表类型进行筛选：All Products（全部）、BestSelling Products、Newest Products | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:42 | Product Analysis 面板 | 筛选区1：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Category：下拉框，类目下拉筛选框 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:43 | Product Analysis 面板 | 筛选区2：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Status：复选按钮，上架状态复选按钮：All（默认）、on sale、out of stock | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:44 | Product Analysis 面板 | 筛选区3：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Ratings：输入框，星级区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:45 | Product Analysis 面板 | 筛选区4：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： reviews：输入框，评论数区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:46 | Product Analysis 面板 | 筛选区5：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Price：输入框，价格区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:47 | Product Analysis 面板 | 筛选区6：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Sales：输入框，近30天销量区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:48 | Product Analysis 面板 | 筛选区7：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Revenues：输入框，近30天销售额区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:49 | Product Analysis 面板 | 筛选区8：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： variants：输入框，页面变体数区间值 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:50 | Product Analysis 面板 | 筛选区9：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Created Time：时间选择器，上架发布时间 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:51 | Product Analysis 面板 | 筛选区10：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Video：复选按钮，是否有视频复选按钮：All（默认）、yes、no | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:52 | Product Analysis 面板 | 筛选区11：对列表字段进行筛选，点击唤起筛选弹窗，弹窗筛选字段如下： Free Shipping：复选按钮，是否免运费复选按钮：All（默认）、yes、no | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:53 | Product Analysis 面板 | 产品列表： SKU：对应商品SKU，交互点击可跳转原网站 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:54 | Product Analysis 面板 | 产品列表： Products Details：包含主图、title、标签：TOP/NEW 、 活动标签（Black Friday...） | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:56 | Product Analysis 面板 | 产品列表： Attributes：SKU的属性 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:57 | Product Analysis 面板 | 产品列表： Sales Price：活动价格 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:58 | Product Analysis 面板 | 产品列表： Price：产品价格 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:59 | Product Analysis 面板 | 产品列表： Sales：销量 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:60 | Product Analysis 面板 | 产品列表： Revenues：销售额 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:61 | Product Analysis 面板 | 产品列表： Ratings：星级评分，包含评分数量和星级 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:62 | Product Analysis 面板 | 产品列表： SKU：对应商品SKU，交互点击可跳转原网站 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:63 | Product Analysis 面板 | 产品列表： Reviews：评论数 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:64 | Product Analysis 面板 | 产品列表： Status：产品状态，分on sale在售和Out of stock下架两种状态 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:65 | Product Analysis 面板 | 产品列表： Category：产品所在类目层级，如：Outdoor / Outdoor Lounge Furniture/ Patio Conversation Sets | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:66 | Product Analysis 面板 | 产品列表： Inventory：库存情况 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:67 | Product Analysis 面板 | 产品列表： Video：是否有视频，格式：yes/no | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:68 | Product Analysis 面板 | 产品列表： Free shipping：是否免费配送，格式：yes/no | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:69 | Product Analysis 面板 | 产品列表： Created Time：产品创建时间，格式：{YY/MM/DD }2022-11-11 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:70 | Product Analysis 面板 | 产品列表： Updated Time：数据更新时间，格式为：{YY/MM/DD 00:00} 2024-11-12 12:36 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:71 | Product Analysis 面板 | 操作： 点击Action的趋势，跳转产品分析页面 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:72 | Product Analysis 面板 | 操作： 导出按钮：可导出本页面/全部商品，格式为 .xlsx ，表头需要和面板表头展示一致；需要根据筛选结果进行 同步导出 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:73 | Sales Promotion面板 | 搜索框：用于输入product title/URL//SKU对列表进行检索 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:74 | Sales Promotion面板 | 时间范围选择器：促销活动抓取时间 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:75 | Sales Promotion面板 | 筛选区域： Type：活动类型下拉筛选 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:76 | Sales Promotion面板 | 筛选区域： 导出按钮：可导出本页面/全部商品，格式为 .xlsx ，表头需要和面板表头展示一致；需要根据筛选结果进行 同步导出 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:77 | Sales Promotion面板 | 产品列表： Updated Time：数据更新时间，格式为：{YY/MM/DD 00:00} 2024-11-12 12:36 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:78 | Sales Promotion面板 | 产品列表： SKU：对应商品SKU，交互点击可跳转原网站 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:79 | Sales Promotion面板 | 产品列表： Products Details：包含主图、title、标签：TOP/NEW 、 活动标签（Black Friday...） | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:81 | Sales Promotion面板 | 产品列表： Name：活动名称，根据页面抓取到的活动名称为准 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:82 | Sales Promotion面板 | 产品列表： Discount：活动折扣（保留页面抓取折扣），如：40% / $33 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:83 | Sales Promotion面板 | 产品列表： Pre- price：活动前价格，如：$81.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:84 | Sales Promotion面板 | 产品列表： Post-price：活动后价格，如：$48.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:85 | Sales Promotion面板 | 产品列表： Threshold：活动门槛，例如：orders over $0.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:86 | Sales Promotion面板 | 产品列表： Start Time：活动开始时间，如：Nov 18,2024 12:00 am | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:87 | Sales Promotion面板 | 产品列表： End Time：活动结束时间：如：Nov 18,2024 12:00 am | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:88 | Sales Promotion面板 | 按照数据更新时间进行排序 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:89 | Sales Promotion面板 | 产品列表同listing页面有多个 SKU 时，取抓到的第一个SKU为listing首行展示，其他SKU作为变体展示 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:90 | Sales Promotion面板 | 促销列表按照活动进行展示，如同个sku有多条活动，页面展示多条 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:91 | Sales Promotion面板 | 该面板仅有权限的人员可查看/编辑 | 前台 /report 和 /app 路由要求登录，报表编辑/导出/自定义动作由 canEdit 按角色门控。 |
| 功能点检查:92 | Sales Trends 面板 | 汇总数据：最新一周或最新一月的汇总数据，根据趋势图的By Month/By Week筛选进行数据展示 30-Day Sales ：上行：当前listing近30天销量，下行：对比上周... | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:93 | Sales Trends 面板 | 汇总数据：最新一周或最新一月的汇总数据，根据趋势图的By Month/By Week筛选进行数据展示 30-Day Revenues：上行：当前listing近30天销售额，下行：对... | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:94 | Sales Trends 面板 | 汇总数据：最新一周或最新一月的汇总数据，根据趋势图的By Month/By Week筛选进行数据展示 Price：该SKU，当前价格 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:95 | Sales Trends 面板 | 汇总数据：最新一周或最新一月的汇总数据，根据趋势图的By Month/By Week筛选进行数据展示 Ratings：该listing的总ratings | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:96 | Sales Trends 面板 | 汇总数据：最新一周或最新一月的汇总数据，根据趋势图的By Month/By Week筛选进行数据展示 Reviews：该listing的总评论数 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:97 | Sales Trends 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周趋势数据查看 Sales | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:98 | Sales Trends 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周趋势数据查看 Revenues（销售额） | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:99 | Sales Trends 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周趋势数据查看 Ratings | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:100 | Sales Trends 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周趋势数据查看 Reviews | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:101 | Sales Trends 面板 | 趋势图：默认按月展示以下关键数据，可通过下拉框进行月/周趋势数据查看 Price：当前SKU的价格趋势 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:102 | Sales Trends 面板 | 筛选区：时间维度下拉选择框，By Month & By Week &By Days | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:103 | Sales Promotion面板 | 搜索框：用于输入product title对列表进行检索 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:104 | Sales Promotion面板 | 时间范围选择器：促销活动抓取时间 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:105 | Sales Promotion面板 | 筛选区域： Type：活动类型下拉筛选 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:106 | Sales Promotion面板 | 筛选区域： SKU：对应商品SKU，下拉输入筛选按钮 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:107 | Sales Promotion面板 | 筛选区域： 导出按钮：可导出本页面/全部商品，格式为 .xlsx ，表头需要和面板表头展示一致；需要根据筛选结果进行 同步导出 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:108 | Sales Promotion面板 | 产品列表： Updated Time：数据更新时间，格式为：{YY/MM/DD 00:00} 2024-11-12 12:36 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:109 | Sales Promotion面板 | 产品列表： SKU：对应商品SKU | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:110 | Sales Promotion面板 | 产品列表： Products Details：包含主图、title、标签：TOP/NEW 、 活动标签（Black Friday...） | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:111 | Sales Promotion面板 | 产品列表： Type：活动类型，分Coupons和Price Promotion两种 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:112 | Sales Promotion面板 | 产品列表： Name：活动名称，根据页面抓取到的活动名称为准 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:113 | Sales Promotion面板 | 产品列表： Discount：活动折扣/折扣价，如：40% / $33 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:114 | Sales Promotion面板 | 产品列表： Pre- price：活动前价格，如：$81.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:115 | Sales Promotion面板 | 产品列表： Post-price：活动后价格，如：$48.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:116 | Sales Promotion面板 | 产品列表： Threshold：活动门槛，例如：orders over $0.00 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:117 | Sales Promotion面板 | 产品列表： Start Time：活动开始时间，如：Nov 18,2024 12:00 am | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:118 | Sales Promotion面板 | 产品列表： End Time：活动结束时间：如：Nov 18,2024 12:00 am | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:119 | Sales Promotion面板 | 按照数据更新时间进行排序 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 功能点检查:120 | Sales Promotion面板 | 同listing页面有多个 SKU 时，取抓到的第一个SKU为listing首行展示，其他SKU作为变体展示 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 问题:5 | 验收问题 | 标杆网站分析报表销售趋势板块里面的数据不可修改，不可查看 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 问题:6 | 验收问题 | 标杆网站分析报表产品分析基于现有功能（很多需求功能缺失），刷新跟导出功能无效，操作选型不可操作 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 问题:7 | 验收问题 | 标杆网站分析报表销售促销搜索无效，类型筛选无效，日期筛选无效，更新时间格式不对 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 问题:8 | 验收问题 | 标杆网站分析报表销售促销板块很多网站都没有数据，实际上标杆网站是有促销的 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |
| 问题:9 | 验收问题 | 采集任务列表没有完成日期 | QueuePage + admin jobs API 已按真实队列表聚合，并暴露运行中、失败、完成时间和详情。 |
| 问题:10 | 验收问题 | 采集任务列表展示52条成功，共60显示，实际上只显示了40条数据 | QueuePage + admin jobs API 已按真实队列表聚合，并暴露运行中、失败、完成时间和详情。 |
| 问题:12 | 验收问题 | 网站分析报表中产品分析模块，点击到最新产品tab，商品无数据，同时所有商品的数量变成0 | SiteReportPage + report/export API 已实现产品分析筛选、列表、导出、趋势和促销明细。 |

## 关闭口径

- 功能类: 代码覆盖后，还需要至少一次前端构建和真实页面点验。
- 数据类: 必须在生产/准生产环境重跑对应站点后，以后台数据质量页和站点报表为准。
- 外部指标类: 流量、转化率必须先导入第三方指标，再刷新报表。
- 代理类: 代理池不是只看配置数量，必须看健康检查、站点规则命中和失败分类。
