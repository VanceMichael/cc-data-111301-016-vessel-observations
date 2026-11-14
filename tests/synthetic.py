"""本地合成 AIS 记录生成器，测试和演练都使用它，不接触无线电设备。"""

DEFECT = {}


def _u(value, width):
    if value < 0 or value >= 1 << width:
        raise ValueError(f'{value} 无法放入 {width} 位')
    return format(value, '0{}b'.format(width))


def _s(value, width):
    if value < 0:
        value += 1 << width
    return _u(value, width)


def _text(value, width):
    value = (value or '')[:width // 6]
    value = value.ljust(width // 6, '@')
    bits = []
    for character in value:
        code = ord(character)
        bits.append(_u(code - 64 if 64 <= code <= 95 else code, 6))
    return ''.join(bits)


def _header(message_type, mmsi, repeat=0):
    return _u(message_type, 6) + _u(repeat, 2) + _u(mmsi, 30)


def encode_type_1(mmsi, latitude=0.0, longitude=0.0, sog=0.0, cog=0.0,
                  heading=0, timestamp=0, nav_status=0):
    lat = _s(round(latitude * 600000), 27)
    lon = _s(round(longitude * 600000), 28)
    bits = (_header(1, mmsi) + _u(nav_status, 4) + _s(0, 8) + _u(round(sog * 10), 10)
            + '0' + lon + lat + _u(round(cog * 10), 12) + _u(heading, 9)
            + _u(timestamp, 6) + _u(0, 2) + '000' + '0' + _u(0, 19))
    assert len(bits) == 168
    return bits


def encode_type_5(mmsi, name, callsign, ship_type=70, destination='', draught=0.0,
                  bow=0, stern=0, port=0, starboard=0, imo=0):
    bits = (_header(5, mmsi) + _u(0, 2) + _u(imo, 30) + _text(callsign, 42)
            + _text(name, 120) + _u(ship_type, 8)
            + _u(bow, 9) + _u(stern, 9) + _u(port, 6) + _u(starboard, 6)
            + _u(0, 4) + _u(0, 4) + _u(0, 5) + _u(0, 5) + _u(0, 6)
            + _u(round(draught * 10), 8) + _text(destination, 120) + '00')
    assert len(bits) == 424
    return bits


def encode_type_18(mmsi, latitude=0.0, longitude=0.0, sog=0.0, cog=0.0, heading=0):
    lat = _s(round(latitude * 600000), 27)
    lon = _s(round(longitude * 600000), 28)
    bits = (_header(18, mmsi) + _u(0, 8) + _u(round(sog * 10), 10) + '0' + lon + lat
            + _u(round(cog * 10), 12) + _u(heading, 9) + _u(0, 6) + _u(0, 2)
            + '00000' + '0' + '0' + _u(0, 20))
    assert len(bits) == 168
    return bits


def encode_type_24a(mmsi, name):
    bits = _header(24, mmsi) + _u(0, 2) + _text(name, 120)
    assert len(bits) == 160
    return bits


def encode_type_24b(mmsi, callsign, ship_type=70, bow=0, stern=0, port=0, starboard=0,
                    vendor='SYN'):
    bits = (_header(24, mmsi) + _u(1, 2) + _u(ship_type, 8) + _text(vendor, 42)
            + _text(callsign, 42) + _u(bow, 9) + _u(stern, 9) + _u(port, 6)
            + _u(starboard, 6) + _u(0, 6))
    assert len(bits) == 168
    return bits


def encode_unsupported(message_type=21, mmsi=412000099):
    """构造一个接收范围之外、但位长合法的报文。"""
    bits = _header(message_type, mmsi) + '0' * (272 - 38)
    assert len(bits) == 272
    return bits


def armoring(bits, fill=None):
    if fill is None:
        fill = (6 - len(bits) % 6) % 6
    padded = bits + '0' * fill
    characters = []
    for offset in range(0, len(padded), 6):
        value = int(padded[offset:offset + 6], 2)
        characters.append(chr(value + 48 + (8 if value >= 40 else 0)))
    return ''.join(characters), fill


def sentence(payload, fill, total=1, part=1, sequence='', channel='A', formatter='AIVDM'):
    body = f'{formatter},{total},{part},{sequence},{channel},{payload},{fill}'
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f'!{body}*{checksum:02X}'


def fragments(bits, sequence='', channel='A', split=None, formatter='AIVDM'):
    """把一条消息拆成多片 NMEA 句子。``split`` 是每片字符数列表。"""
    payload, fill = armoring(bits)
    if split is None:
        half = len(payload) // 2
        split = [half, len(payload) - half]
    if sum(split) != len(payload):
        raise ValueError('分片长度与 payload 字符数不一致')
    lines = []
    offset = 0
    for number, length in enumerate(split, 1):
        piece = payload[offset:offset + length]
        offset += length
        piece_fill = fill if number == len(split) else 0
        lines.append(sentence(piece, piece_fill, total=len(split), part=number,
                              sequence=sequence, channel=channel, formatter=formatter))
    return lines


def single(bits, sequence='', channel='A'):
    payload, fill = armoring(bits)
    return [sentence(payload, fill, sequence=sequence, channel=channel)]


def write_sources(root, sources, interleaved=True):
    """写出连接清单和来源文件，返回清单路径。

    ``sources`` 是 ``{station: {'connection': int, 'lines': [...]}}``。
    """
    import json
    from pathlib import Path
    root = Path(root)
    entries = []
    for station, source in sources.items():
        name = f'{station}.nmea'
        (root / name).write_text('\n'.join(source['lines']) + '\n', encoding='ascii')
        entries.append({'file': name, 'station': station, 'connection': source['connection']})
    path = root / 'connections.json'
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    return path
