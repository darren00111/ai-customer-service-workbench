"""Bounded decoder for the PDD browser's Titan receive protocol.

Wire layout was checked against Customer-Agent's published protocol notes
(scripts/decode_pdd_ws.py, commit 4343a1f). This implementation only observes;
the original browser handles authentication, heartbeats and acknowledgements.
"""
import base64
import binascii
import json
import struct
import zlib

LIMIT = 1024 * 1024


class WireError(ValueError):
    pass


def _number(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise WireError('消息数字字段被截断')
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise WireError('消息数字字段超出64位')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise WireError('消息数字字段过长')


def _fields(data):
    fields, offset = [], 0
    if len(data) > LIMIT:
        raise WireError('消息负载过大')
    while offset < len(data):
        if len(fields) >= 256:
            raise WireError('消息字段过多')
        key, offset = _number(data, offset)
        tag, kind = key >> 3, key & 7
        if not 1 <= tag < 2**29:
            raise WireError('消息字段标识无效')
        if kind == 0:
            value, offset = _number(data, offset)
        else:
            if kind == 2:
                length, offset = _number(data, offset)
            elif kind in (1, 5):
                length = 8 if kind == 1 else 4
            else:
                raise WireError('消息使用尚未支持的编码')
            if length > len(data) - offset:
                raise WireError('消息字段长度不完整')
            value = data[offset:offset + length]
            offset += length
        fields.append((tag, kind, value))
    return fields


def _one(fields, tag, kind, required=True):
    matches = [v for t, k, v in fields if t == tag and k == kind]
    if len(matches) != 1:
        if not matches and not required:
            return None
        raise WireError('消息缺少字段或含重复身份字段')
    return matches[0]


def _inflate(data):
    if not data.startswith(b'\x1f\x8b'):
        return data
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        plain = decoder.decompress(data, LIMIT + 1)
    except zlib.error as exc:
        raise WireError('压缩消息已损坏') from exc
    if len(plain) > LIMIT or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise WireError('压缩消息过大或不完整')
    return plain


def _notifications(value):
    if not isinstance(value, dict):
        raise WireError('通知正文格式无效')
    if value.get('response') in ('push', 'auth', 'system_push', 'mall_system_msg'):
        return [value]
    if value.get('push_type') != 2:
        return []
    data = value.get('push_data')
    if not isinstance(data, dict) or not isinstance(data.get('data'), list) or len(data['data']) > 100:
        raise WireError('聊天通知列表格式无效')
    messages = []
    for item in data['data']:
        if not isinstance(item, dict):
            raise WireError('聊天通知条目无效')
        if item.get('chat_type_id') != 1:
            continue
        if not isinstance(item.get('message'), dict):
            raise WireError('聊天通知缺少消息')
        messages.append({'response': 'push', 'message': item['message']})
    return messages


def receive_envelopes(payload, opcode):
    """Return canonical push envelopes, or [] for a known non-chat event."""
    if not isinstance(payload, str) or len(payload) > 2 * LIMIT:
        raise WireError('消息报文过大')
    try:
        if opcode == 1:
            if len(payload.encode('utf-8')) > LIMIT:
                raise WireError('文字报文过大')
            return _notifications(json.loads(payload))
        if opcode in (8, 9, 10):
            return []
        if opcode != 2:
            raise WireError('未知消息帧类型')
        raw = base64.b64decode(payload, validate=True)
        if not 16 <= len(raw) <= LIMIT + 16:
            raise WireError('二进制消息长度无效')
        version, channel, correlation, flags, size = struct.unpack('>HHIII', raw[:16])
        if size != len(raw) - 16:
            raise WireError('二进制消息长度不匹配')
        if version == 0:
            return []  # Browser-owned heartbeat; never a buyer question.
        if version != 10 or channel != 102:
            raise WireError('消息协议版本尚未适配')
        outer = _fields(raw[16:])
        method = _one(outer, 1, 2).decode('utf-8')
        if method in ('titan.session', 'titan.sync', 'titan.notifyDataLite.ack'):
            return []
        if method != 'titan.notifyDataLite':
            raise WireError('消息通道返回未知方法')
        inner = _fields(_inflate(_one(outer, 10, 2)))
        business = _one(inner, 6, 2, required=False)
        if business is None:
            return []
        return _notifications(json.loads(business.decode('utf-8')))
    except (binascii.Error, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WireError('消息编码或正文无法解析') from exc
