"""Bilibili WBI 签名（search / 创作者主页等接口需要）。

参考 B站官方 wbi 签名机制：从 nav 接口取 img_key/sub_key → 混淆得到 mixin_key →
对请求参数加 wts(时间戳) 后排序 urlencode，再 md5 得到 w_rid。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from functools import reduce

import httpx

# B站官方混淆索引表
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]


def _get_mixin_key(orig: str) -> str:
    """用混淆表重排 img_key+sub_key，截取前 32 位作为 mixin_key。"""
    return reduce(lambda s, i: s + orig[i], _MIXIN_KEY_ENC_TAB, "")[:32]


async def get_wbi_keys(client: httpx.AsyncClient) -> tuple[str, str]:
    """从 nav 接口获取 (img_key, sub_key)。"""
    resp = (await client.get("https://api.bilibili.com/x/web-interface/nav")).json()
    img_url = resp["data"]["wbi_img"]["img_url"]
    sub_url = resp["data"]["wbi_img"]["sub_url"]
    img_key = img_url.rsplit("/", 1)[1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def sign_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """对参数做 WBI 签名，返回带 wts / w_rid 的新参数 dict。"""
    mixin_key = _get_mixin_key(img_key + sub_key)
    params = {**params, "wts": int(time.time())}
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params
