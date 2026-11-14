"""NMEA AIVDM 包络的读取与校验。

本模块只负责句子级别的包络：校验和、字段数量、分片编号、填充位和
原始 payload。AIS 的六位编码在 :mod:`vessel_observations.ais` 中解码，
多片消息的组装在 :mod:`vessel_observations.fragments` 中完成。
"""

from pathlib import Path


def _is_payload_character(character):
    """AIS payload 使用的 NMEA 六位编码字符范围。"""
    value = ord(character)
    return 48 <= value <= 87 or 96 <= value <= 119


def parse_sentence(line, line_number=None):
    """解析并校验一条 NMEA 句子。

    返回的 ``raw`` 保留去掉行尾空白后的原文，``payload`` 是该片段的
    armoured 文本，尚未做六位解码。
    """
    text = line.strip()
    if not text.startswith('!') or '*' not in text:
        raise ValueError('NMEA 包络不合法')
    body, checksum_text = text[1:].rsplit('*', 1)
    if len(checksum_text) != 2 or any(character not in '0123456789abcdefABCDEF' for character in checksum_text):
        raise ValueError('NMEA 校验和格式不正确')
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    if checksum != int(checksum_text, 16):
        raise ValueError('NMEA 校验和不一致')

    fields = body.split(',')
    if len(fields) != 7:
        raise ValueError('NMEA 字段数量不正确')
    formatter = fields[0]
    if not formatter:
        raise ValueError('NMEA formatter 为空')
    try:
        total = int(fields[1])
        part = int(fields[2])
    except ValueError as error:
        raise ValueError('NMEA 分片编号不是整数') from error
    if total < 1 or part < 1 or part > total:
        raise ValueError('NMEA 分片编号超出范围')
    sequence = fields[3]
    if sequence and not sequence.isdigit():
        raise ValueError('NMEA 顺序号不是数字')
    channel = fields[4]
    payload = fields[5]
    if not payload:
        raise ValueError('NMEA payload 为空')
    if any(not _is_payload_character(character) for character in payload):
        raise ValueError('NMEA payload 含非法六位编码字符')
    try:
        fill = int(fields[6])
    except ValueError as error:
        raise ValueError('NMEA 填充位不是整数') from error
    if fill < 0 or fill > 5:
        raise ValueError('NMEA 填充位超出范围')
    if part < total and fill != 0:
        raise ValueError('非末片段不应有填充位')
    if len(payload) * 6 - fill <= 0:
        raise ValueError('NMEA payload 位长不足')
    return {
        'line': line_number,
        'raw': text,
        'formatter': formatter,
        'total': total,
        'part': part,
        'sequence': sequence,
        'channel': channel,
        'payload': payload,
        'fill': fill,
    }


def read_sentences(path):
    """逐行读取严格校验过的 AIS NMEA 句子。"""
    with Path(path).open(encoding='ascii') as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError('NMEA 空行')
            yield parse_sentence(line, line_number=line_number)


def inspect_file(path):
    sentences = list(read_sentences(path))
    return {'sentences': len(sentences), 'multipart': sum(item['total'] > 1 for item in sentences)}
