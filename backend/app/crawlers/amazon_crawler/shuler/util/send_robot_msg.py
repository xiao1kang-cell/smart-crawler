"""告警"""
import base64
import hashlib
import hmac
import logging
import time
import urllib

from loguru import logger

access_token = '8d92dfd491b7ae528b1235cd7b77ed235a72ff605f950c4780696439590b3c23'



def send_custom_robot_group_message( msg, at_mobiles=None,at_user_ids=None, is_at_all=False,access_token = access_token):
    """
    发送钉钉自定义机器人群消息
    :param access_token: 机器人webhook的access_token
    :param secret: 机器人安全设置的加签secret
    :param msg: 消息内容
    :param at_user_ids: @的用户ID列表
    :param at_mobiles: @的手机号列表
    :param is_at_all: 是否@所有人
    :return: 钉钉API响应
    """
    import requests
    timestamp = str(round(time.time() * 1000))
    url = f'https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}'

    body = {
        "at": {
            "isAtAll": str(is_at_all).lower(),
            "atUserIds": at_user_ids or [],
            "atMobiles": at_mobiles or []
        },
        "text": {
            "content": '告警：'+msg
        },
        "msgtype": "text"
    }
    headers = {'Content-Type': 'application/json'}
    resp = requests.post(url, json=body, headers=headers, timeout=10)
    logger.info(f"钉钉自定义机器人群消息响应：{resp.text}")
    return resp.json()

if __name__ == '__main__':
    send_custom_robot_group_message('测试信息',at_mobiles=['17398238551'])
