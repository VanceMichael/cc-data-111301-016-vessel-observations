"""AIS 六位编码解码与消息字段提取。

只解析接收范围明确支持的报文：位置报告 1/2/3/18、完整静态资料 5、
分开传输的静态资料 24A/24B。其余类型保留原文，不构造船舶资料。
"""


class DecodeError(ValueError):
    """payload 无法按声明的报文类型解码。"""


# AIS 六位编码字符表：48..87 和 96..119 是有效字符，中间 88..95 不使用。
def armoring_value(character):
    code = ord(character)
    if 48 <= code <= 87:
        return code - 48
    if 96 <= code <= 119:
        return code - 56
    raise DecodeError('payload 字符超出六位编码范围')


def payload_bits(payload, fill=0):
    """把 armoured payload 展开成比特串，并去掉末尾填充位。"""
    if not payload:
        raise DecodeError('payload 为空')
    bits = ''.join(format(armoring_value(character), '06b') for character in payload)
    if fill < 0 or fill > 5:
        raise DecodeError('填充位超出范围')
    if fill > len(bits):
        raise DecodeError('填充位超过 payload 位长')
    return bits[:-fill] if fill else bits


def _uint(bits, start, length):
    chunk = bits[start:start + length]
    if len(chunk) != length:
        raise DecodeError('payload 位长不足')
    return int(chunk, 2)


def _sint(bits, start, length):
    value = _uint(bits, start, length)
    if value & (1 << (length - 1)):
        value -= 1 << length
    return value


def _text(bits, start, length):
    if length % 6:
        raise DecodeError('文本字段位长不是六的倍数')
    value = []
    for offset in range(start, start + length, 6):
        code = _uint(bits, offset, 6)
        value.append(chr(code + 64) if code < 32 else chr(code))
    text = ''.join(value).replace('@', ' ')
    return text.strip() or None


SHIP_TYPE_NAMES = {
    30: 'fishing', 31: 'towing', 32: 'towing (large)', 33: 'dredging',
    34: 'diving operation', 35: 'military operation', 36: 'sailing',
    37: 'pleasure craft', 50: 'pilot vessel', 51: 'search and rescue',
    52: 'tug', 53: 'port tender', 54: 'anti-pollution equipment',
    55: 'law enforcement', 58: 'medical transport', 59: 'non-combatant ship',
}


def _ship_type_text(code):
    if 20 <= code <= 29:
        return 'wing in ground'
    if 40 <= code <= 49:
        return 'high speed craft'
    if 60 <= code <= 69:
        return 'passenger'
    if 70 <= code <= 79:
        return 'cargo'
    if 80 <= code <= 89:
        return 'tanker'
    if 90 <= code <= 99:
        return 'other'
    return SHIP_TYPE_NAMES.get(code)


def _dimensions(bits, start):
    bow = _uint(bits, start, 9)
    stern = _uint(bits, start + 9, 9)
    port = _uint(bits, start + 18, 6)
    starboard = _uint(bits, start + 24, 6)
    if not any((bow, stern, port, starboard)):
        return None
    return {
        'to_bow': bow,
        'to_stern': stern,
        'to_port': port,
        'to_starboard': starboard,
        'length': bow + stern,
        'beam': port + starboard,
    }


def _position_values(bits, sog_start, accuracy_start, lon_start, lat_start,
                     cog_start, heading_start, timestamp_start):
    if len(bits) < timestamp_start + 6:
        raise DecodeError('位置报告位长不足')
    raw_lon = _sint(bits, lon_start, 28)
    raw_lat = _sint(bits, lat_start, 27)
    # 181°/91° 是 AIS 的“不可用”哨兵值；越界值也按不可用处理。
    longitude = None if abs(raw_lon) > 180 * 600000 else raw_lon / 600000.0
    latitude = None if abs(raw_lat) > 90 * 600000 else raw_lat / 600000.0
    raw_sog = _uint(bits, sog_start, 10)
    raw_cog = _uint(bits, cog_start, 12)
    raw_heading = _uint(bits, heading_start, 9)
    raw_timestamp = _uint(bits, timestamp_start, 6)
    return {
        'latitude': latitude,
        'longitude': longitude,
        'position_available': latitude is not None and longitude is not None,
        'speed_over_ground': None if raw_sog >= 1023 else raw_sog / 10.0,
        'course_over_ground': None if raw_cog >= 3600 else raw_cog / 10.0,
        'true_heading': None if raw_heading >= 511 or raw_heading > 359 else raw_heading,
        'timestamp': None if raw_timestamp >= 60 else raw_timestamp,
        'position_accuracy': bool(_uint(bits, accuracy_start, 1)),
    }


def _rate_of_turn(raw):
    if raw == -128:
        return None
    if raw == 127:
        return 720.0
    if raw == -127:
        return -720.0
    return (1 if raw >= 0 else -1) * (abs(raw) / 4.733) ** 2


NAVIGATION_STATUS = {
    0: 'under way using engine', 1: 'at anchor', 2: 'not under command',
    3: 'restricted manoeuvrability', 4: 'constrained by draught',
    5: 'moored', 6: 'aground', 7: 'engaged in fishing',
    8: 'under way sailing', 9: 'reserved (hazmat A)', 10: 'reserved (hazmat B)',
    11: 'reserved (hazmat C)', 12: 'reserved (hazmat D)', 13: 'reserved',
    14: 'AIS-SART active', 15: 'not defined',
}


def _header(bits):
    if len(bits) < 38:
        raise DecodeError('payload 位长不足以读取报文头和 MMSI')
    return {
        'message_type': _uint(bits, 0, 6),
        'repeat_indicator': _uint(bits, 6, 2),
        'mmsi': _uint(bits, 8, 30),
    }


def _decode_type_123(bits):
    fields = _header(bits)
    if len(bits) < 168:
        raise DecodeError('位置报告位长不足')
    nav = _uint(bits, 38, 4)
    rot = _sint(bits, 42, 8)
    fields.update(_position_values(bits, 50, 60, 61, 89, 116, 128, 137))
    fields.update({
        'navigation_status': nav,
        'navigation_status_text': NAVIGATION_STATUS.get(nav),
        'rate_of_turn': _rate_of_turn(rot),
        'maneuver_indicator': _uint(bits, 143, 2),
        'raim': bool(_uint(bits, 148, 1)),
        'radio_status': _uint(bits, 149, 19),
    })
    return fields


def _decode_type_18(bits):
    fields = _header(bits)
    if len(bits) < 168:
        raise DecodeError('Class B 位置报告位长不足')
    fields.update(_position_values(bits, 46, 56, 57, 85, 112, 124, 133))
    fields.update({
        'reserved': _uint(bits, 38, 8),
        'class_b_unit_flag': bool(_uint(bits, 139, 1)),
        'class_b_display': bool(_uint(bits, 140, 1)),
        'class_b_dsc': bool(_uint(bits, 141, 1)),
        'class_b_band': bool(_uint(bits, 142, 1)),
        'class_b_message22': bool(_uint(bits, 143, 1)),
        'assigned_mode': bool(_uint(bits, 144, 1)),
        'raim': bool(_uint(bits, 147, 1)),
        'radio_status': _uint(bits, 148, 20),
    })
    return fields


def _decode_type_5(bits):
    fields = _header(bits)
    if len(bits) < 424:
        raise DecodeError('完整静态资料位长不足')
    imo = _uint(bits, 40, 30)
    ship_type = _uint(bits, 232, 8)
    eta_month = _uint(bits, 274, 4)
    eta_day = _uint(bits, 278, 5)
    eta_hour = _uint(bits, 283, 5)
    eta_minute = _uint(bits, 288, 6)
    eta = None
    if 1 <= eta_month <= 12 and 1 <= eta_day <= 31 and eta_hour <= 23 and eta_minute <= 59:
        eta = {'month': eta_month, 'day': eta_day, 'hour': eta_hour, 'minute': eta_minute}
    draught = _uint(bits, 294, 8)
    fields.update({
        'ais_version': _uint(bits, 38, 2),
        'imo_number': imo or None,
        'callsign': _text(bits, 70, 42),
        'name': _text(bits, 112, 120),
        'ship_type': ship_type or None,
        'ship_type_text': _ship_type_text(ship_type) if ship_type else None,
        'dimensions': _dimensions(bits, 240),
        'epfd_type': _uint(bits, 270, 4),
        'eta': eta,
        'draught': None if draught == 0 else draught / 10.0,
        'destination': _text(bits, 302, 120),
        'dte': bool(_uint(bits, 422, 1)),
    })
    return fields


def _decode_type_24(bits):
    fields = _header(bits)
    if len(bits) < 40:
        raise DecodeError('静态资料位长不足')
    part = _uint(bits, 38, 2)
    fields['part_number'] = part
    if part == 0:
        if len(bits) < 160:
            raise DecodeError('24A 静态资料位长不足')
        fields['part_label'] = 'A'
        fields['name'] = _text(bits, 40, 120)
    elif part == 1:
        if len(bits) < 168:
            raise DecodeError('24B 静态资料位长不足')
        ship_type = _uint(bits, 40, 8)
        fields.update({
            'part_label': 'B',
            'ship_type': ship_type or None,
            'ship_type_text': _ship_type_text(ship_type) if ship_type else None,
            'vendor_id': _text(bits, 48, 42),
            'callsign': _text(bits, 90, 42),
            'dimensions': _dimensions(bits, 132),
        })
    else:
        raise DecodeError('24 静态资料的分部编号不是 A/B')
    return fields


SUPPORTED_TYPES = {1, 2, 3, 5, 18, 24}

MESSAGE_NAMES = {
    1: 'position_report_class_a',
    2: 'position_report_class_a',
    3: 'position_report_class_a',
    5: 'static_and_voyage',
    18: 'position_report_class_b',
    24: 'static_data_report',
}

POSITION_TYPES = {1, 2, 3, 18}
STATIC_TYPES = {5, 24}


def decode_bits(bits):
    """按报文类型解码。返回 ``supported`` 与字段；未知类型不猜字段。"""
    if len(bits) < 6:
        raise DecodeError('payload 位长不足以读取报文类型')
    message_type = _uint(bits, 0, 6)
    result = {
        'message_type': message_type,
        'message_type_name': MESSAGE_NAMES.get(message_type, 'unsupported'),
        'supported': message_type in SUPPORTED_TYPES,
    }
    if not result['supported']:
        result['fields'] = {}
        result['reason'] = '不支持的消息类型，保留原文'
        return result
    if message_type in (1, 2, 3):
        result['fields'] = _decode_type_123(bits)
    elif message_type == 18:
        result['fields'] = _decode_type_18(bits)
    elif message_type == 5:
        result['fields'] = _decode_type_5(bits)
    else:
        result['fields'] = _decode_type_24(bits)
    result['supported'] = True
    return result


def decode_payload(payload, fill=0):
    """解码单片 payload。多片消息应先拼接后再调用。"""
    return decode_bits(payload_bits(payload, fill))
