import json
import time

from app.crawlers.amazon_crawler.shuler.services.amazon.reviews import Reviews
from app.crawlers.amazon_crawler.shuler.util.mongo_ import MongoAccountDB
from app.crawlers.amazon_crawler.shuler.util.mysql_ import MySQLTaskDB
from loguru import logger


def del_error_data():
    """从 MySQL 读取异常数据，重新解析后存入 reviews 表，成功后删除"""
    mysql_db = MySQLTaskDB()
    # 获取待处理的异常数据
    errors = mysql_db.get_pending_errors(limit=100)

    if not errors:
        logger.info("没有待处理的异常数据")
        return

    logger.info(f"获取到 {len(errors)} 条待处理异常数据")

    for error in errors:
        error_id = error['id']
        task_info = error.get('task_info', {})
        resp_data = error.get('resp', '')

        try:
            # 解析 resp（可能是 JSON 字符串或纯 HTML）
            if isinstance(resp_data, str):
                try:
                    resp_obj = json.loads(resp_data)
                except json.JSONDecodeError:
                    resp_obj = {'resp': resp_data, 'url': ''}
            else:
                resp_obj = resp_data

            # 构建 reviews 对象（用假账号，因为只需要解析方法）
            review = Reviews({'username': 'reprocess'}, task=task_info)

            # 重新解析
            seen_ids = set()
            reviews,token = review.parse_reviews_ajax(resp_obj, task_info, seen_ids)

            if reviews:
                db = MongoAccountDB()
                db.db['reviews_temp'].insert_many(reviews)
                db.client.close()  # 用完即关，防止后台线程积累
                # 保存到 reviews 表（如果有的话，这里只是演示逻辑）
                logger.info(f"✅ ASIN {task_info.get('asin', 'unknown')} 重新解析成功，共 {len(reviews)} 条评论")
                # 实际项目中这里可能需要 insert 到某个结果表
                # 标记为已处理（或者删除）
                mysql_db.delete_error(error_id)
                logger.info(f"🗑️ 已删除处理完成的异常记录 id={error_id}")
            else:
                logger.warning(f"⚠️ ASIN {task_info.get('asin', 'unknown')} 重新解析无数据")
                # 标记为已处理但无结果
                mysql_db.mark_error_processed(error_id)

        except Exception as e:
            logger.error(f"❌ 处理异常记录 id={error_id} 失败: {str(e)}")
            # 保留记录，下次可重试

    mysql_db.close()
    logger.info("异常数据处理完成")


if __name__ == '__main__':
    del_error_data()

